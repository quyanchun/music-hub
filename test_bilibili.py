"""Bilibili AU adapter boundaries; all upstream responses are simulated."""
import json
import unittest
from unittest.mock import MagicMock, patch

from resolver import NAMES, resolve
import bilibili
from public_sources import list_playlists, playlist_tracks


class BilibiliTests(unittest.TestCase):
    def track(self, **extra):
        return {'id': 2445151, 'title': '原创 &amp; 合唱', 'author': '作者',
                'duration': 197, 'limit': 0, **extra}

    def test_featured_playlists_and_pagination_use_au_not_video_api(self):
        with patch('bilibili.api', return_value={'data': [{'menuId': 10624, 'title': '新曲推荐'}]}):
            self.assertEqual(list_playlists('bilibili')[0]['id'], '10624')
        with patch('bilibili.api', return_value={'totalSize': 35, 'data': [self.track()]}) as api:
            page = playlist_tracks('bilibili', '10624', 30)
            api.assert_called_once_with('song/of-menu', {'sid': '10624', 'ps': 30, 'pn': 2})
        self.assertFalse(page['has_more'])
        self.assertEqual(page['songs'][0]['title'], '原创 & 合唱')
        self.assertEqual(page['songs'][0]['duration_seconds'], 197)
        self.assertFalse(bilibili.track(self.track(limit=1))['playable'])

    def test_general_search_uses_local_catalog_and_explicit_au_lookup_is_supported(self):
        with patch('bilibili.api', return_value=self.track()) as api:
            self.assertEqual(bilibili.search('原创'), [])
            api.assert_not_called()
            self.assertEqual(bilibili.search('https://www.bilibili.com/audio/au2445151')[0]['id'], '2445151')
            api.assert_called_once_with('song/info', {'sid': '2445151'})

    def test_resolution_uses_m4a_and_actual_cdn_not_unreliable_api_size(self):
        song = bilibili.track(self.track())
        with patch('bilibili.api', side_effect=[self.track(),
                {'sid': 2445151, 'size': 168, 'cdns': ['https://upos.bilivideo.com/audio.m4a']} ]):
            url, headers, suffix = resolve(song)
        self.assertEqual(suffix, 'm4a')
        self.assertEqual(headers['Referer'], 'https://www.bilibili.com/')
        self.assertTrue(url.endswith('.m4a'))
        self.assertIn('bilibili', NAMES)
        for body in ({'sid': 1, 'cdns': ['https://upos.bilivideo.com/a']},
                     {'sid': 2445151, 'cdns': []},
                     {'sid': 2445151, 'cdns': ['https://evil.example/a.m4a']}):
            with self.subTest(body=body), patch('bilibili.api', side_effect=[self.track(), body]):
                with self.assertRaises(ValueError): resolve(song)
        with patch('bilibili.api', return_value=self.track(limit=1)) as api:
            with self.assertRaises(ValueError): resolve(song)
            self.assertEqual(api.call_count, 1)

    def test_api_rejects_errors_and_oversized_responses(self):
        response = MagicMock()
        response.__enter__.return_value = response
        for body in ({'code': -1, 'data': None}, {'code': 0, 'data': []}):
            response.iter_content.return_value = [json.dumps(body).encode()]
            with patch('bilibili.requests.get', return_value=response):
                with self.assertRaises(ValueError): bilibili.fetch('url', {'sid': '1'})
        response.iter_content.return_value = [b'x' * (2 * 1024 * 1024 + 1)]
        with patch('bilibili.requests.get', return_value=response):
            with self.assertRaises(ValueError): bilibili.fetch('url', {'sid': '1'})
