"""Protocol and filtering regressions; no remote music APIs are used."""
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

from catalog import Catalog, identifier, utc
from contextlib import redirect_stdout
from resolver import blocked
from service import App, authenticated


def record(key='1', paid=False):
    return {'source': 'netease', 'id': key, 'title': '测试歌曲' + key,
            'singer': '测试歌手', 'album': '测试专辑', 'duration_seconds': 180,
            'playable': not paid, 'downloadable': not paid, 'availability': '需会员' if paid else ''}


def insert(catalog, song, status='accepted'):
    key = identifier(song['source'], song['id'])
    with catalog.connect() as db:
        db.execute('INSERT INTO songs(id,data,status,media,checked) VALUES (?,?,?,?,?)',
                   (key, json.dumps(song), status, json.dumps({'suffix': 'mp3', 'size': 10240, 'duration': 180, 'bitrate': 128}), utc()))
    return key


class HTTPFixture(unittest.TestCase):
    def setUp(self):
        search = patch('online_search.source_search', return_value=[])
        search.start()
        self.addCleanup(search.stop)
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.app = App(('127.0.0.1', 0), self.state.name)
        self.addCleanup(self.app.server_close)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.shutdown)
        self.ids = [insert(self.app.catalog, record('1')), insert(self.app.catalog, record('2'))]
        self.paid = insert(self.app.catalog, record('3', True), 'rejected')
        with self.app.catalog.connect() as db:
            db.execute('INSERT INTO boards VALUES (?,?,?,?,?,?)',
                       ('netease-hot', 'netease', '网易云 · 热歌榜', 3, json.dumps(self.ids + [self.paid]), utc()))
        self.auth = {'u': self.app.account['username'], 'p': self.app.account['password'], 'v': '1.16.1', 'c': 'test', 'f': 'json'}
        self.audio = b'ID3' + b'abcdefghij' * 1024
        for key in self.ids:
            (self.app.media.folder / (key + '.mp3')).write_bytes(self.audio)

    def request(self, endpoint, params=None, headers=None, method='GET', auth=True):
        values = {**(self.auth if auth else {}), **(params or {})}
        connection = http.client.HTTPConnection('127.0.0.1', self.app.server_port, timeout=5)
        try:
            data = urlencode(values, doseq=True)
            if method == 'POST':
                connection.request('POST', '/rest/' + endpoint, data, {'Content-Type': 'application/x-www-form-urlencoded', **(headers or {})})
            else:
                connection.request(method, '/rest/' + endpoint + '?' + data, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def api(self, endpoint, params=None, **kwargs):
        _, _, body = self.request(endpoint, params, **kwargs)
        return json.loads(body)['subsonic-response']


class ProtocolTests(HTTPFixture):
    def test_login_variants_and_xml(self):
        self.assertEqual(self.api('ping')['status'], 'ok')
        salt = 'random-salt'
        token = hashlib.md5((self.auth['p'] + salt).encode()).hexdigest()
        self.assertEqual(self.api('ping.view', {'p': '', 't': token, 's': salt})['status'], 'ok')
        self.assertEqual(self.api('ping', {'p': 'enc:' + self.auth['p'].encode().hex()})['status'], 'ok')
        self.assertEqual(self.api('ping', {'p': 'wrong'})['error']['code'], 40)
        self.assertEqual(self.api('ping', {'f': 'json'}, auth=False)['error']['code'], 40)
        _, headers, body = self.request('ping.view', {'f': 'xml'})
        self.assertTrue(headers['Content-Type'].startswith('text/xml'))
        self.assertEqual(ET.fromstring(body).attrib['status'], 'ok')
        self.assertEqual(self.api('ping', method='POST')['status'], 'ok')
        self.assertEqual((Path(self.state.name) / 'credentials.json').stat().st_mode & 0o777, 0o600)

    def test_diagnostic_logs_exclude_passwords_and_tokens(self):
        captured = io.StringIO()
        salt = 'private-salt-value'
        token = hashlib.md5((self.auth['p'] + salt).encode()).hexdigest()
        with redirect_stdout(captured):
            self.api('ping.view')
            self.api('ping', {'s': salt, 't': token})
            self.api('ping', {'p': 'private-wrong-password'})
            connection = http.client.HTTPConnection('127.0.0.1', self.app.server_port, timeout=5)
            try:
                connection.request('POST', '/auth/login?password=private-query-password',
                                   'private-body-password', {'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
            finally:
                connection.close()
        logs = captured.getvalue()
        self.assertIn('"path": "/auth/login"', logs)
        self.assertIn('"status": 404', logs)
        self.assertIn('"code": 40', logs)
        for secret in (self.auth['p'], salt, token, 'private-wrong-password',
                       'private-query-password', 'private-body-password'):
            self.assertNotIn(secret, logs)

    def test_browse_objects_and_cover(self):
        self.assertEqual(len(self.api('getMusicFolders')['musicFolders']['musicFolder']), 5)
        artists = self.api('getArtists')['artists']['index'][0]['artist']
        artist = self.api('getArtist', {'id': artists[0]['id']})['artist']
        album = self.api('getAlbum', {'id': artist['album'][0]['id']})['album']
        self.assertEqual(album['songCount'], 2)
        self.assertEqual({s['id'] for s in album['song']}, set(self.ids))
        self.assertEqual(self.api('getSong', {'id': self.ids[0]})['song']['id'], self.ids[0])
        self.assertEqual(len(self.api('getMusicDirectory', {'id': album['id']})['directory']['child']), 2)
        _, headers, raw = self.request('getCoverArt', {'id': album['coverArt']})
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertTrue(raw.startswith(b'\x89PNG\r\n\x1a\n'))

    def test_search_pagination_and_paid_exclusion(self):
        first = self.api('search3', {'query': '测试', 'songCount': 1})['searchResult3']['song']
        second = self.api('search3', {'query': '测试', 'songCount': 1, 'songOffset': 1})['searchResult3']['song']
        self.assertNotEqual(first[0]['id'], second[0]['id'])
        self.assertEqual(len(self.api('search3', {'query': '""', 'songCount': 500})['searchResult3']['song']), 2)
        self.assertEqual(self.api('search3', {'query': 'not-there'})['searchResult3']['song'], [])
        self.assertEqual(self.api('getSong', {'id': self.paid})['error']['code'], 70)
        self.assertEqual(self.api('search3', {'query': '*', 'songCount': 10000})['error']['code'], 10)

    def test_playlists_preserve_rank_and_hide_paid(self):
        playlist = self.api('getPlaylists')['playlists']['playlist'][0]
        self.assertEqual(playlist['songCount'], 2)
        entries = self.api('getPlaylist', {'id': playlist['id']})['playlist']['entry']
        self.assertEqual([s['id'] for s in entries], self.ids)
        self.assertEqual(self.api('getPlaylist', {'id': 'missing'})['error']['code'], 70)

    def test_audio_range_head_and_auth(self):
        status, headers, body = self.request('stream.view', {'id': self.ids[0]}, {'Range': 'bytes=0-1023'})
        self.assertEqual((status, len(body)), (206, 1024))
        self.assertEqual(headers['Content-Range'], f'bytes 0-1023/{len(self.audio)}')
        self.assertEqual(body, self.audio[:1024])
        status, _, body = self.request('stream', {'id': self.ids[0]}, {'Range': 'bytes=-8'})
        self.assertEqual((status, body), (206, self.audio[-8:]))
        status, headers, body = self.request('stream', {'id': self.ids[0]}, method='HEAD')
        self.assertEqual((status, len(body), int(headers['Content-Length'])), (200, 0, len(self.audio)))
        self.assertEqual(self.request('stream', {'id': self.ids[0]}, {'Range': 'bytes=999999-'})[0], 416)
        self.assertEqual(self.request('stream', {'id': self.ids[0]}, {'Range': 'bytes=0-2', 'If-Range': 'old'})[0], 200)
        self.assertEqual(self.api('stream', {'id': self.ids[0], 'p': 'wrong'})['error']['code'], 40)
        self.assertEqual(self.api('stream', {'id': self.paid})['error']['code'], 70)
        self.assertEqual(self.api('stream', {'id': '../credentials'})['error']['code'], 70)
        self.assertIn('attachment', self.request('download', {'id': self.ids[0]})[1]['Content-Disposition'])

    def test_favorites_rating_and_play_count_are_persistent(self):
        self.api('star', {'id': self.ids[0]})
        self.assertEqual(len(self.api('getStarred2')['starred2']['song']), 1)
        self.api('setRating', {'id': self.ids[0], 'rating': 4})
        self.api('scrobble', {'id': self.ids[0]})
        song = self.api('getSong', {'id': self.ids[0]})['song']
        self.assertEqual((song['userRating'], song['playCount']), (4, 1))
        self.api('unstar', {'id': self.ids[0]})
        self.assertEqual(self.api('getStarred2')['starred2']['song'], [])
        reopened = Catalog(self.state.name)
        self.assertEqual(reopened.visible()[0][0]['rating'], 4)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.catalog = Catalog(self.state.name)

    def test_full_chart_pagination_filter_and_resume(self):
        def board(source, chart, page):
            return {'songs': [record('1'), record('2', True)] if page == 1 else [record('3')],
                    'name': '热歌榜', 'total': 3, 'has_more': page == 1}
        with patch('catalog.BOARDS', {'netease': {'hot': 1}}), patch('catalog.get_chart', side_effect=board), patch('catalog.probe', return_value={'suffix': 'mp3', 'duration': 180}) as probe:
            self.catalog.scan(2)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(self.catalog.status()['counts'], {'accepted': 2, 'rejected': 1})
            self.catalog.scan(2)
            self.assertEqual(probe.call_count, 2)
        self.assertEqual(json.loads(self.catalog.visible()[1][0]['members']), [identifier('netease', n) for n in ('1', '2', '3')])

    def test_failed_probe_never_enters_music_library(self):
        board = {'songs': [record()], 'name': 'test', 'total': 1, 'has_more': False}
        with patch('catalog.BOARDS', {'netease': {'hot': 1}}), patch('catalog.get_chart', return_value=board), patch('catalog.probe', side_effect=ValueError('trial')):
            self.catalog.scan(1)
        self.assertEqual(self.catalog.visible()[0], [])
        self.assertEqual(self.catalog.status()['counts'], {'unavailable': 1})

    def test_rejected_track_can_become_free_on_refresh(self):
        insert(self.catalog, record('1', True), 'rejected')
        board = {'songs': [record('1')], 'name': 'test', 'total': 1, 'has_more': False}
        with patch('catalog.BOARDS', {'netease': {'hot': 1}}), patch('catalog.get_chart', return_value=board), patch('catalog.probe', return_value={'duration': 180}):
            self.catalog.scan(1)
        self.assertEqual(self.catalog.status()['counts'], {'accepted': 1})

    def test_connections_are_closed_during_bulk_scan(self):
        before = len(os.listdir('/dev/fd'))
        for _ in range(1200):
            with self.catalog.connect() as db:
                db.execute('SELECT count(*) FROM songs').fetchone()
        self.assertLess(len(os.listdir('/dev/fd')), before + 8)

    def test_explicit_membership_and_trial_flags(self):
        self.assertTrue(blocked(record(paid=True)))
        self.assertTrue(blocked({'playable': True, 'downloadable': False}))
        self.assertTrue(blocked({'availability': '试听片段'}))
        self.assertFalse(blocked(record()))


if __name__ == '__main__':
    unittest.main()
