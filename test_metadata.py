"""Client playback-page regressions. All metadata provider requests are mocked."""
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from catalog import Catalog
from library import Library
from metadata import MetadataCache, allowed_url, matching_netease, structured_lyrics
from service import xml_response
from test_service import HTTPFixture, insert, record

SAMPLE_LRC = '[ar:测试歌手]\n[offset:120]\n[00:01.25][00:03.50]测试第一行\n[00:02.00]测试第二行'


class MetadataTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.catalog = Catalog(state.name)
        self.key = insert(self.catalog, record())
        self.cache = MetadataCache(self.catalog)

    def test_lrc_multistamp_offset_and_xml(self):
        result = structured_lyrics(SAMPLE_LRC, '测试歌手', '测试歌曲')
        self.assertEqual([line['start'] for line in result[0]['line']], [1250, 2000, 3500])
        self.assertEqual(result[0]['offset'], 120)
        self.assertTrue(result[0]['synced'])
        xml = ET.fromstring(xml_response({'status': 'ok', 'lyricsList': {'structuredLyrics': result}}))
        lines = xml.findall('.//{*}line')
        self.assertEqual(lines[0].text, '测试第一行')
        self.assertEqual(lines[0].attrib['start'], '1250')
        self.assertEqual(structured_lyrics('', 'a', 'b'), [])
        self.assertFalse(structured_lyrics('plain lyric', 'a', 'b')[0]['synced'])

    def test_metadata_is_persistent_and_cached(self):
        with patch('metadata.source_metadata', return_value={'lrc': SAMPLE_LRC, 'cover_url': ''}) as fetch:
            self.assertEqual(self.cache.get(self.key)['lrc'], SAMPLE_LRC)
            self.cache.get(self.key)
            reopened = MetadataCache(Catalog(self.catalog.folder))
            self.assertEqual(reopened.get(self.key)['lrc'], SAMPLE_LRC)
            self.assertEqual(fetch.call_count, 1)

    def test_exact_indexed_copy_is_used_before_global_search(self):
        alternate = record()
        alternate['source'] = 'kugou'
        insert(self.catalog, alternate)
        def response(fetch, song):
            return {'lrc': SAMPLE_LRC if song['source'] == 'kugou' else '', 'cover_url': ''}
        with patch('metadata.source_metadata', side_effect=response), patch('metadata.matching_netease') as search:
            result = self.cache.get(self.key)
        self.assertEqual(result['lrc'], SAMPLE_LRC)
        self.assertEqual(result['matched_source'], 'kugou')
        search.assert_not_called()

    def test_missing_metadata_returns_empty_and_negative_cache(self):
        with patch('metadata.source_metadata', side_effect=TimeoutError) as fetch:
            self.assertEqual(self.cache.get(self.key)['lrc'], '')
            self.assertEqual(self.cache.get(self.key)['lrc'], '')
            self.assertEqual(fetch.call_count, 1)
            with self.catalog.connect() as db:
                db.execute('UPDATE metadata SET checked=?', (time.time() - 601,))
            self.cache.get(self.key)
            self.assertEqual(fetch.call_count, 2)

    def test_wrong_artist_live_version_and_duration_are_not_matched(self):
        class Fake:
            def __init__(self, name, artist, duration):
                self.name, self.artist, self.duration = name, artist, duration
            def json(self, *args):
                return {'result': {'songs': [{'id': 7, 'name': self.name,
                    'artists': [{'name': self.artist}], 'duration': self.duration * 1000}]}}
        song = record()
        self.assertIsNone(matching_netease(Fake(song['title'], '另一个歌手', 180), song))
        self.assertIsNone(matching_netease(Fake(song['title'] + '(Live)', song['singer'], 180), song))
        self.assertIsNone(matching_netease(Fake(song['title'], song['singer'], 60), song))
        self.assertEqual(matching_netease(Fake(song['title'], song['singer'], 181), song), 7)

    def test_untrusted_cover_url_and_html_are_rejected(self):
        self.assertFalse(allowed_url('http://127.0.0.1/private'))
        self.assertFalse(allowed_url('https://music.163.com.evil.example/image'))
        with patch('metadata.source_metadata', return_value={'lrc': '<html>error</html>', 'cover_url': 'http://127.0.0.1/image'}):
            result = self.cache.get(self.key)
            self.assertEqual((result['lrc'], result['cover_url']), ('', ''))


class PlaybackPageTests(HTTPFixture):
    # Reuse the real HTTP fixture while keeping this module focused on new cases.
    def test_lyrics_and_open_subsonic_responses(self):
        with patch('metadata.source_metadata', return_value={'lrc': SAMPLE_LRC, 'cover_url': ''}):
            text = self.api('getLyrics', {'artist': '测试歌手', 'title': '测试歌曲1'})['lyrics']['value']
            self.assertEqual(text, SAMPLE_LRC)
            structured = self.api('getLyricsBySongId', {'id': self.ids[0]})['lyricsList']['structuredLyrics']
            self.assertEqual(len(structured[0]['line']), 3)
            _, _, body = self.request('getLyrics', {'artist': '测试歌手', 'title': '测试歌曲1', 'f': 'xml'})
            self.assertIn('测试第一行', ET.fromstring(body).find('{*}lyrics').text)
        self.assertEqual(self.api('getLyrics', {'title': 'missing'})['lyrics']['value'], '')
        self.assertEqual(self.api('getLyricsBySongId', {'id': 'missing'})['lyricsList']['structuredLyrics'], [])
        ext = self.api('getOpenSubsonicExtensions')['openSubsonicExtensions']
        self.assertEqual(ext, [{'name': 'songLyrics', 'versions': [1]}])

    def test_similar_song_artist_album_and_missing_id(self):
        similar = self.api('getSimilarSongs', {'id': self.ids[0]})['similarSongs']['song']
        self.assertEqual([s['id'] for s in similar], [self.ids[1]])
        artist = self.api('getSong', {'id': self.ids[0]})['song']['artistId']
        self.assertEqual(self.api('getSimilarSongs2', {'id': artist})['status'], 'ok')
        self.assertEqual(self.api('getSimilarSongs', {'id': 'missing'})['similarSongs']['song'], [])

    def test_top_songs_order_and_collaboration(self):
        extra = record('4')
        extra['singer'] = '测试歌手、合作者'
        insert(self.app.catalog, extra)
        top = self.api('getTopSongs', {'artist': '测试歌手'})['topSongs']['song']
        self.assertEqual([s['id'] for s in top[:2]], self.ids)
        self.assertEqual(len(top), 3)
        self.assertEqual(self.api('getTopSongs', {'artist': 'missing'})['topSongs']['song'], [])

    def test_duplicate_recordings_are_not_recommended_as_new_songs(self):
        duplicate = record('1')
        duplicate['source'] = 'kuwo'
        insert(self.app.catalog, duplicate)
        library = Library(self.app.catalog)
        similar = library.similar_songs(self.ids[0])
        self.assertEqual([s['id'] for s in similar], [self.ids[1]])

    def test_stale_cover_ids_are_images_not_protocol_errors(self):
        for key in ('al-stale-client-id', 'pl-netease-hot', 'unknown', '../credentials'):
            status, headers, raw = self.request('getCoverArt', {'id': key})
            self.assertEqual((status, headers['Content-Type']), (200, 'image/png'))
            self.assertTrue(raw.startswith(b'\x89PNG'))
