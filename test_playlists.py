"""Public collection permissions, pagination and standard client integration."""
import json
import time
import unittest
from unittest.mock import patch

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
    def refresh(self, listing, pages):
        with patch('public_playlists.NAMES', {'netease': '网易云'}), \
             patch('public_playlists.list_playlists', return_value=listing), \
             patch('public_playlists.playlist_tracks', side_effect=pages), \
             patch('public_playlists.probe', return_value={'suffix': 'mp3', 'duration': 180, 'size': 4096000}):
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
