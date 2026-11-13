"""Search admission, client playback IDs, cache stability and trial rejection."""
from concurrent.futures import ThreadPoolExecutor
import json
import time
from unittest.mock import MagicMock, patch

from catalog import Catalog, identifier
from online_search import source_search
from resolver import probe
from test_service import HTTPFixture, record, insert

MEDIA = {'suffix': 'mp3', 'size': 2880000, 'duration': 180,
         'bitrate': 128, 'duration_verified': True}


class OnlineTests(HTTPFixture):
    def test_online_results_persist_and_play_through_client_endpoints(self):
        song = record('outside')
        with patch('online_search.source_search', return_value=[song]) as search, patch('online_search.probe', return_value=MEDIA) as check:
            found = self.api('search3', {'query': 'outside'})['searchResult3']['song']
            self.assertEqual(len(found), 1)
            key = found[0]['id']
            self.assertEqual(search.call_count, 6)
            check.assert_called_once_with(song, verify_duration=True)
            self.assertEqual(self.api('getSong', {'id': key})['song']['id'], key)
            self.assertTrue(self.api('getAlbum', {'id': found[0]['albumId']})['album']['song'])
            (self.app.media.folder / (key + '.mp3')).write_bytes(self.audio)
            status, _, raw = self.request('stream.view', {'id': key}, {'Range': 'bytes=0-31'})
            self.assertEqual((status, raw), (206, self.audio[:32]))
            self.api('star', {'id': key})
            self.assertTrue(self.api('getSong', {'id': key})['song']['starred'])
            self.assertEqual(Catalog(self.state.name).song(key)['id'], 'outside')
            # Query snapshot is persisted across search-instance/server restarts.
            from online_search import OnlineSearch
            reopened = OnlineSearch(Catalog(self.state.name))
            try:
                self.assertEqual(reopened.search('outside'), [key])
            finally:
                reopened.close()
            self.assertEqual(search.call_count, 6)

    def test_paid_trial_unknown_and_failed_source_never_admitted(self):
        free, trial, paid, unknown = [record(n) for n in ('free', 'trial', 'paid', 'unknown')]
        paid.update(playable=False, availability='需会员')
        unknown['duration_seconds'] = 0
        def search(source, query):
            if source == 'qq':
                raise TimeoutError()
            return [free, trial, paid, unknown] if source == 'netease' else []
        def check(song, **kwargs):
            if song['id'] == 'trial':
                raise ValueError('short_preview_or_duration_mismatch')
            return MEDIA
        with patch('online_search.source_search', side_effect=search), patch('online_search.probe', side_effect=check):
            songs = self.api('search3', {'query': 'free'})['searchResult3']['song']
        self.assertEqual([s['title'] for s in songs], [free['title']])
        for song in (trial, paid, unknown):
            self.assertEqual(self.api('getSong', {'id': identifier(song['source'], song['id'])})['error']['code'], 70)

    def test_folder_pagination_cache_and_empty_queries(self):
        songs = [record('new' + str(n)) for n in range(3)]
        with patch('online_search.source_search', return_value=songs) as search, patch('online_search.probe', return_value=MEDIA):
            params = {'query': 'new', 'musicFolderId': '1', 'songCount': 1}
            first = self.api('search3', params)['searchResult3']['song']
            second = self.api('search2.view', {**params, 'songOffset': 1})['searchResult2']['song']
            self.assertNotEqual(first[0]['id'], second[0]['id'])
            self.assertEqual(search.call_count, 1)
            self.api('search3', {'query': 'new', 'musicFolderId': '1', 'songOffset': 3})
            for query in ('', '*', '""'):
                self.api('search3', {'query': query})
            self.assertEqual(search.call_count, 1)
            self.assertEqual(self.api('search3', {'query': 'new', 'songCount': -1})['error']['code'], 10)
            self.assertEqual(self.api('search3', {'query': 'x' * 161})['error']['code'], 10)
            self.assertEqual(search.call_count, 1)

    def test_new_membership_and_playback_exclusion_hide_cached_ids(self):
        song = record('1', True)
        with patch('online_search.source_search', return_value=[song]):
            found = self.api('search3', {'query': '测试歌曲1'})['searchResult3']['song']
            self.assertEqual(found, [])
        song = record('fresh')
        with patch('online_search.source_search', return_value=[song]), patch('online_search.probe', return_value=MEDIA):
            found = self.api('search3', {'query': 'fresh'})['searchResult3']['song']
            self.app.catalog.exclude(found[0]['id'], 'short_preview_at_playback')
            self.assertEqual(self.api('search3', {'query': 'fresh'})['searchResult3']['song'], [])

    def test_slow_sources_do_not_hold_request_open(self):
        def slow(*args):
            time.sleep(.15)
            return []
        with patch('online_search.SEARCH_SECONDS', .03), patch('online_search.source_search', side_effect=slow):
            started = time.monotonic()
            self.assertEqual(self.api('search3', {'query': 'slow'})['searchResult3']['song'], [])
            self.assertLess(time.monotonic() - started, .12)

    def test_identical_concurrent_queries_share_network_work(self):
        song = record('shared')
        def search(*args):
            time.sleep(.02)
            return [song]
        with patch('online_search.source_search', side_effect=search) as network, patch('online_search.probe', return_value=MEDIA):
            with ThreadPoolExecutor(max_workers=2) as pool:
                answers = list(pool.map(lambda _: self.app.search.search('shared'), range(2)))
            self.assertEqual(answers[0], answers[1])
            self.assertEqual(network.call_count, 6)


class ProbeTests(__import__('unittest').TestCase):
    def response(self):
        response = MagicMock()
        response.headers = {'Content-Range': 'bytes 0-4095/2880000'}
        response.raw.read.return_value = b'ID3' + b'a' * 4093
        response.__enter__.return_value = response
        return response

    def test_real_duration_rejects_unlabelled_trial_and_non_audio(self):
        for duration, streams, accepted in [(45, [{'codec_type': 'audio'}], False),
                                             (180, [], False), (180, [{'codec_type': 'audio'}], True)]:
            result = MagicMock(stdout=json.dumps({'format': {'duration': duration}, 'streams': streams}))
            with patch('resolver.resolve', return_value=('https://music.126.net/test.mp3', {}, 'mp3')), patch('resolver.requests.get', return_value=self.response()), patch('resolver.subprocess.run', return_value=result):
                if accepted:
                    self.assertTrue(probe(record(), verify_duration=True)['duration_verified'])
                else:
                    with self.assertRaises(ValueError):
                        probe(record(), verify_duration=True)

    def test_netease_preserves_membership_and_trial_resolver_flags(self):
        response = MagicMock()
        response.json.return_value = {'code': 200, 'result': {'songs': [
            {'id': 1, 'name': 'free', 'duration': 180000, 'fee': 0, 'artists': [{'name': '歌手'}]},
            {'id': 2, 'name': 'paid', 'duration': 180000, 'fee': 1}]}}
        with patch('online_search.requests.get', return_value=response):
            songs = source_search('netease', 'anything')
        self.assertTrue(songs[0]['playable'])
        self.assertFalse(songs[1]['playable'])
        from resolver import resolve
        with patch('music_dl.addons.netease.NeteaseApi.request', return_value={'data': [{'url': 'https://music.126.net/a.mp3', 'freeTrialInfo': {'start': 0, 'end': 45}}]}):
            with self.assertRaises(ValueError):
                resolve(songs[0])
