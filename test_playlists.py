"""Public collection permissions, pagination and standard client integration."""
import json
import time
import unittest
from unittest.mock import MagicMock, patch
import hashlib

from catalog import identifier
from public_sources import playlist_tracks
from test_service import HTTPFixture, record


class SourcePermissions(unittest.TestCase):
    def tracks(self, source, body):
        with patch('public_sources.fetch', return_value=body):
            return playlist_tracks(source, '123', 0)['songs']

    def test_netease_membership_and_paid_are_excluded(self):
        tracks = [{'id': n, 'name': 'song', 'fee': n, 'duration': 180000} for n in (0, 1, 4, 8)]
        songs = self.tracks('netease', {'result': {'tracks': tracks, 'trackCount': 100}})
        self.assertEqual([s['playable'] for s in songs], [True, False, False, True])
        with patch('public_sources.fetch', return_value={'result': {'tracks': tracks, 'trackCount': 100}}):
            page = playlist_tracks('netease', '123', 0)
            self.assertTrue(page['limited'])
            self.assertFalse(page['has_more'])

    def test_qq_paid_and_kugou_standard_quality_flags(self):
        songs = self.tracks('qq', {'cdlist': [{'songlist': [
            {'songmid': 'free', 'pay': {'payplay': 0}}, {'songmid': 'paid', 'pay': {'pay_play': 1}}]}]})
        self.assertEqual([s['playable'] for s in songs], [True, False])
        songs = self.tracks('kugou', {'data': {'total': 2, 'info': [
            {'hash': 'free', 'pay_type': 0, 'pay_type_sq': 3, 'filename': '歌手 - 歌曲'},
            {'hash': 'paid', 'pay_type': 3}]}})
        self.assertEqual([s['playable'] for s in songs], [True, False])
        self.assertEqual((songs[0]['title'], songs[0]['singer']), ('歌曲', '歌手'))

    def test_kuwo_vip_quality_is_not_a_full_play_restriction(self):
        rows = [{'id': 'free', 'payInfo': {'play': '1'}},
                {'id': 'trial', 'payInfo': {'listen_fragment': '1'}},
                {'id': 'offline', 'online': '0'}, {'id': 'blocked', 'payInfo': {'cannotOnlinePlay': '1'}}]
        self.assertEqual([s['playable'] for s in self.tracks('kuwo', {'musiclist': rows})], [True, False, False, False])

    def test_migu_pq_vip_and_private_qq_response(self):
        rows = [{'contentId': '1', 'audioFormats': [{'formatType': 'SQ', 'showTags': ['vip']}]},
                {'contentId': '2', 'audioFormats': [{'formatType': 'PQ', 'showTags': ['vip']}]}]
        self.assertEqual([s['playable'] for s in self.tracks('migu', {'data': {'songList': rows}})], [True, False])
        with patch('public_sources.fetch', return_value={'code': 0, 'subcode': 4000}):
            with self.assertRaises(ValueError):
                playlist_tracks('qq', '123', 0)


class PublicPlaylistTests(HTTPFixture):
    def cache(self, key):
        path = self.app.media.folder / (key + '.mp3')
        path.write_bytes(self.audio + key.encode())
        return self.app.media.register(key, self.app.catalog.song(key, allow_unavailable=True),
                                       path, 180, hashlib.sha256(path.read_bytes()).hexdigest())

    def refresh(self, listing, pages):
        with patch('public_playlists.NAMES', {'netease': '网易云'}), \
             patch('public_playlists.list_playlists', return_value=listing), \
             patch('public_playlists.playlist_tracks', side_effect=pages):
            self.app.public_playlists.refresh()

    def test_playlist_publish_rank_stable_ids_filter_and_no_browsing_network(self):
        card = {'id': 'public-1', 'source': 'netease', 'name': '公开歌单', 'count': 3}
        self.refresh([card], [{'songs': [record('2'), record('3', True), record('1')], 'total': 3, 'has_more': False}])
        with patch('public_playlists.playlist_tracks', side_effect=AssertionError('network during browse')):
            lists = self.api('getPlaylists')['playlists']['playlist']
            playlist = next(p for p in lists if p['id'].startswith('pl-'))
            entries = self.api('getPlaylist', {'id': playlist['id']})['playlist']['entry']
        self.assertEqual(playlist['songCount'], 2)
        self.assertEqual([s['id'] for s in entries], list(reversed(self.ids)))
        self.assertEqual(len(lists), 3)  # Existing chart remains available.
        _, _, xml = self.request('getPlaylist', {'id': playlist['id'], 'f': 'xml'})
        self.assertIn(b'<entry ', xml)

    def test_paid_first_page_does_not_hide_free_later_pages(self):
        card = {'id': 'later', 'source': 'netease', 'name': '后页可播'}
        self.refresh([card], [
            {'songs': [record('paid', True)], 'total': 31, 'has_more': True},
            {'songs': [record('new'), record('new')], 'total': 31, 'has_more': False}])
        public = next(p for p in self.api('getPlaylists')['playlists']['playlist'] if p['id'].startswith('pl-'))
        self.assertEqual(public['songCount'], 1)
        self.assertEqual(self.api('getPlaylist', {'id': public['id']})['playlist']['entry'][0]['id'], identifier('netease', 'new'))

    def test_fully_restricted_and_unreadable_playlists_are_hidden(self):
        cards = [{'id': str(n), 'source': 'netease', 'name': '歌单' + str(n)} for n in range(2)]
        self.refresh(cards, [{'songs': [record('paid', True)], 'total': 1, 'has_more': False}, ValueError('private')])
        self.assertEqual(len(self.api('getPlaylists')['playlists']['playlist']), 2)

    def test_dead_source_snapshot_expires_and_maintenance_preserves_live_playlist_songs(self):
        card = {'id': 'live', 'source': 'netease', 'name': '保留歌单'}
        self.refresh([card], [{'songs': [record('new')], 'total': 1, 'has_more': False}])
        with self.app.catalog.connect() as db:
            db.execute('UPDATE songs SET touched=?', (time.time() - 40 * 86400,))
        self.app.maintenance.run()
        self.assertEqual(self.app.catalog.song(identifier('netease', 'new'))['title'], '测试歌曲new')
        with self.app.catalog.connect() as db:
            db.execute('UPDATE public_playlist_catalog SET updated=?', (time.time() - 90000,))
        with patch('public_playlists.NAMES', {'netease': '网易云'}), patch('public_playlists.list_playlists', side_effect=ValueError('offline')):
            self.app.public_playlists.refresh()
        self.assertEqual(len(self.api('getPlaylists')['playlists']['playlist']), 2)

    def test_audio_aac_range_mime_and_persistence(self):
        key = self.ids[0]
        old = self.app.media.folder / (key + '.mp3')
        target = old.with_suffix('.aac')
        old.rename(target)
        status, headers, raw = self.request('stream', {'id': key}, {'Range': 'bytes=0-1023'})
        self.assertEqual((status, headers['Content-Type'], len(raw)), (206, 'audio/aac', 1024))
        import os
        os.utime(target, (time.time() - 90000,) * 2)
        self.app.maintenance.run()
        self.assertTrue(target.exists())

    def test_new_playlist_is_visible_without_network_then_manual_play_is_shared_cache(self):
        card = {'id': 'new', 'source': 'netease', 'name': '新歌单'}
        with patch('resolver.probe', side_effect=AssertionError('eager audio request')):
            self.refresh([card], [{'songs': [record('untried')], 'total': 1, 'has_more': False}])
        playlist = next(p for p in self.api('getPlaylists')['playlists']['playlist'] if p['id'].startswith('pl-'))
        entry = self.api('getPlaylist', {'id': playlist['id']})['playlist']['entry'][0]
        self.assertFalse(entry['id'].startswith('cache-'))
        self.assertEqual(self.api('getSong', {'id': entry['id']})['song']['title'], entry['title'])
        response = MagicMock()
        response.headers = {'Content-Length': '12'}
        response.__enter__.return_value = response
        response.iter_content.return_value = [b'ID3abcdefghi']
        with patch('media.resolve', return_value=('https://music.126.net/audio', {}, 'mp3')), \
             patch('media.requests.get', return_value=response), \
             patch('media.inspect_audio', return_value={'format': {'duration': 180}, 'streams': [{'codec_type': 'audio'}]}):
            self.assertEqual(self.request('stream', {'id': entry['id']})[0], 200)
        cached = self.api('getPlaylist', {'id': playlist['id']})['playlist']['entry'][0]
        self.assertEqual(cached['id'], 'cache-' + entry['id'])
        self.assertEqual(self.app.media.summary()['count'], 1)
        with patch('media.resolve', side_effect=AssertionError('cached playback went online')):
            self.assertEqual(self.request('stream', {'id': cached['id']}, {'Range': 'bytes=0-2'})[2], b'ID3')
            self.assertEqual(self.request('download', {'id': cached['id']}, method='HEAD')[0], 200)

    def test_failed_tracks_hidden_counts_match_and_refresh_does_not_resurrect(self):
        card = {'id': 'partial', 'source': 'netease', 'name': '部分缓存'}
        page = {'songs': [record(str(n)) for n in range(100)], 'total': 100, 'has_more': False}
        self.refresh([card], [page])
        for n in range(75): self.cache(identifier('netease', str(n)))
        for n in range(75, 100): self.app.prefetch.retry(identifier('netease', str(n)), ValueError('failed'))
        self.refresh([card], [page])
        public = next(p for p in self.api('getPlaylists')['playlists']['playlist'] if p['id'].startswith('pl-'))
        body = self.api('getPlaylist', {'id': public['id']})['playlist']
        self.assertEqual((public['songCount'], body['songCount'], len(body['entry'])), (75, 75, 75))
        self.assertTrue(all(s['id'].startswith('cache-') for s in body['entry']))
        # A background retry that actually succeeds restores the track.
        self.cache(identifier('netease', '75'))
        self.assertEqual(self.api('getPlaylist', {'id': public['id']})['playlist']['songCount'], 76)

    def test_cache_and_untried_stay_visible_failed_and_empty_charts_hidden(self):
        self.cache(self.ids[0])
        self.app.catalog.exclude(self.ids[1], 'audio_download_failed')
        lists = self.api('getPlaylists')['playlists']['playlist']
        self.assertEqual([p['songCount'] for p in lists], [1, 1])
        self.app.catalog.exclude(self.ids[0], 'audio_download_failed')
        # A good cached file remains usable even if the remote source fails.
        self.assertEqual(self.api('getPlaylist', {'id': 'netease-hot'})['playlist']['songCount'], 1)
        (self.app.media.folder / (self.ids[0] + '.mp3')).unlink()
        self.assertEqual(len(self.api('getPlaylists')['playlists']['playlist']), 1)

    def test_manual_failed_download_hides_track_but_memory_pause_does_not(self):
        from memory_budget import BackgroundPaused
        key = self.ids[0]
        (self.app.media.folder / (key + '.mp3')).unlink()
        with patch('media.resolve', side_effect=ValueError('audio missing')):
            with self.assertRaises(ValueError): self.app.media.prepare(key)
        self.assertNotIn(key, [row['key'] for row in self.app.catalog.playlist_rows('netease-hot')])
        (self.app.media.folder / (self.ids[1] + '.mp3')).unlink()
        self.app.media.players = 1
        with self.assertRaises(BackgroundPaused): self.app.media.prepare(self.ids[1], background=True)
        self.app.media.players = 0
        self.assertIn(self.ids[1], [row['key'] for row in self.app.catalog.playlist_rows('netease-hot')])
