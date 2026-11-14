"""可读缓存文件名与「歌曲名 + 歌手名」去重的回归测试。"""
import hashlib
import os
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from catalog import Catalog
from media import MediaCache, display_name
from test_service import HTTPFixture, insert, record


class DisplayNameTests(unittest.TestCase):
    def test_example_and_illegal_characters(self):
        self.assertEqual(display_name({'title': '精卫', 'singer': '王悦辰'}), '精卫 - 王悦辰.mp3')
        self.assertEqual(display_name({'title': 'A/B:C*D?E"F<G>H|I', 'singer': '歌手'}, '.m4a'),
                         'A·B·C·D·E·F·G·H·I - 歌手.m4a')
        self.assertEqual(display_name({'title': '', 'artist': ''}), '未知歌曲 - 未知歌手.mp3')
        self.assertEqual(display_name({'title': '  精卫  ', 'singer': '  王悦辰  '}), '精卫 - 王悦辰.mp3')

    def test_utf8_byte_cap(self):
        name = display_name({'title': '歌' * 200, 'singer': '歌手'})
        self.assertLessEqual(len(name.encode('utf-8')), 130)
        self.assertTrue(name.endswith('.mp3'))
        self.assertGreater(len(name), 10)


class CacheNameTests(HTTPFixture):
    def setUp(self):
        super().setUp()
        # 基类会放两个未登记的 32 位 ID 文件；这些用例只关心自己创建的缓存。
        for path in self.app.media.folder.iterdir():
            path.unlink()

    def song(self, key, title='精卫', singer='王悦辰', source='netease', duration=180):
        data = record(key)
        data.update(source=source, title=title, singer=singer, duration_seconds=duration)
        return insert(self.app.catalog, data), data

    def download(self, key):
        response = MagicMock()
        response.headers = {'Content-Length': '12'}
        response.iter_content.return_value = [b'ID3', b'abcdefghi']
        response.__enter__.return_value = response
        info = {'format': {'duration': 180}, 'streams': [{'codec_type': 'audio'}]}
        with patch('media.resolve', return_value=('https://music.example/audio', {}, 'mp3')), \
             patch('media.requests.get', return_value=response), \
             patch('media.inspect_audio', return_value=info):
            return self.app.media.prepare(key)

    def test_download_uses_readable_name_and_still_streams(self):
        key, _ = self.song('download-1')
        path = self.download(key)
        self.assertEqual(path.name, '精卫 - 王悦辰.mp3')
        self.assertTrue(path.is_file())
        with self.app.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT path FROM audio_files WHERE id=?', (key,)).fetchone()['path'],
                             '精卫 - 王悦辰.mp3')
        self.assertEqual(self.request('stream', {'id': key}, {'Range': 'bytes=0-2'})[0::2], (206, b'ID3'))
        status, headers, _ = self.request('download', {'id': key}, method='HEAD')
        self.assertEqual(status, 200)
        self.assertIn(quote('精卫 - 王悦辰.mp3'), headers['Content-Disposition'])

    def test_same_title_and_artist_is_one_file_even_with_different_duration(self):
        first, _ = self.song('same-a')
        path = self.download(first)
        second, _ = self.song('same-b', title='精 卫', source='kuwo', duration=240)
        with patch('media.resolve', side_effect=AssertionError('同名同歌手不应重复下载')):
            self.assertEqual(self.app.media.prepare(second), path)
        self.assertEqual(self.app.media.summary()['count'], 1)
        with self.app.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT file_id FROM audio_aliases WHERE song_id=?', (second,)).fetchone()['file_id'], first)
        other, data = self.song('other', title='精卫', singer='另一位歌手')
        self.assertIsNone(self.app.media.equivalent(data))

    def test_legacy_id_files_are_migrated_at_startup_and_idempotent(self):
        key = self.ids[0]
        old = self.app.media.folder / (key + '.mp3')
        old.write_bytes(self.audio)
        data = self.app.catalog.song(key)
        self.app.media.register(key, data, old, 180, hashlib.sha256(self.audio).hexdigest())
        self.assertTrue(old.exists())

        reopened = MediaCache(Catalog(self.state.name))
        expected = display_name(data)
        self.assertFalse(old.exists())
        self.assertTrue((self.app.media.folder / expected).is_file())
        with self.app.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT path FROM audio_files WHERE id=?', (key,)).fetchone()['path'], expected)
        self.assertEqual(reopened.migrate_names(), 0)
        # 迁移后仍可正常播放，内部 id 不变
        self.assertEqual(self.request('stream', {'id': key}, {'Range': 'bytes=0-2'})[0::2], (206, b'ID3'))

    def test_cleanup_keeps_registered_readable_files_but_removes_orphans(self):
        key, _ = self.song('cleanup-1')
        path = self.download(key)
        orphan = self.app.media.folder / '手动放进去的孤儿文件.mp3'
        orphan.write_bytes(b'orphan')
        old = (time.time() - 90000)
        os.utime(path, (old,) * 2)
        os.utime(orphan, (old,) * 2)
        self.app.media.cleanup()
        self.assertTrue(path.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(self.app.media.summary()['count'], 1)

    def test_reconcile_recovers_readable_orphan_file(self):
        key = self.ids[0]
        data = self.app.catalog.song(key)
        path = self.app.media.folder / display_name(data)
        path.write_bytes(self.audio)
        with self.app.catalog.connect() as db:
            db.execute('INSERT INTO audio_files VALUES (?,?,?,?,?,?,?,?)',
                       (key, '', 180, path.name, path.stat().st_size, '', path.stat().st_mtime, 0))
        info = {'format': {'duration': 180}, 'streams': [{'codec_type': 'audio'}]}
        with patch('media.inspect_audio', return_value=info), patch('media.background_allowed', return_value=True):
            while self.app.media.reconcile_one():
                pass
        self.assertIsNotNone(self.app.media.cached(key, verified=True))
        self.assertEqual(self.request('stream', {'id': key}, {'Range': 'bytes=0-2'})[0::2], (206, b'ID3'))


if __name__ == '__main__':
    unittest.main()
