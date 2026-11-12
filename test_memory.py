"""Regressions for bounded transient memory without persistent object caches."""
from concurrent.futures import ThreadPoolExecutor
import gc
import json
import sys
import threading
import time
from unittest.mock import MagicMock, patch

from library import Library
from resolver import AudioInspector, inspect_audio
from test_service import HTTPFixture


class MemoryTests(HTTPFixture):
    def test_single_song_and_small_endpoints_do_not_build_entire_library(self):
        with patch('service.Library', wraps=Library) as library:
            self.api('getMusicFolders')
            self.api('star', {'id': self.ids[0]})
            library.assert_not_called()
            self.assertEqual(self.api('getSong', {'id': self.ids[0]})['song']['id'], self.ids[0])
            library.assert_called_once_with(self.app.catalog, [self.ids[0]])
        with self.app.view_slots:
            self.assertEqual(self.api('ping')['status'], 'ok')
            self.assertEqual(self.api('getSong', {'id': self.ids[0]})['song']['id'], self.ids[0])
        with patch.object(self.app.catalog, 'visible', side_effect=AssertionError('eager catalog load')):
            self.assertEqual(len(Library(self.app.catalog).songs), 2)
        self.assertNotIn('music_dl.__main__', sys.modules)

    def test_cover_lookup_uses_disk_records_and_preserves_aliases(self):
        song = self.api('getSong', {'id': self.ids[0]})['song']
        for key in (song['id'], song['albumId'], song['artistId'], 'al-' + song['albumId'],
                    'ar-' + song['artistId'], 'pl-netease-hot'):
            self.assertIn(self.ids[0], self.app.catalog.cover_keys(key))
            with patch('service.Library', side_effect=AssertionError('cover must not build library')):
                status, headers, image = self.request('getCoverArt', {'id': key})
            self.assertEqual(status, 200)
            self.assertTrue(headers['Content-Type'].startswith('image/'))
            self.assertTrue(image)
        self.assertEqual(self.app.catalog.cover_keys('stale'), [])

    def test_finished_song_locks_are_not_retained_and_sqlite_is_bounded(self):
        self.app.media.prepare(self.ids[0])
        with patch('metadata.source_metadata', return_value={'lrc': '[00:01]test', 'cover_url': ''}), patch('metadata.matching_netease', return_value=None):
            self.app.metadata.get(self.ids[0])
        gc.collect()
        self.assertEqual(len(self.app.media.locks), 0)
        self.assertEqual(len(self.app.metadata.locks), 0)
        with self.app.catalog.connect() as db:
            self.assertEqual(db.execute('PRAGMA cache_size').fetchone()[0], -128)
            self.assertEqual(db.execute('PRAGMA temp_store').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA mmap_size').fetchone()[0], 0)

    def test_playback_and_search_inspections_share_one_process_budget(self):
        count = {'active': 0, 'maximum': 0, 'calls': 0}
        guard = threading.Lock()
        def run(*args, **kwargs):
            with guard:
                count['active'] += 1
                count['calls'] += 1
                count['maximum'] = max(count['active'], count['maximum'])
            time.sleep(.015)
            with guard:
                count['active'] -= 1
            return MagicMock(stdout=json.dumps({'format': {'duration': 180}, 'streams': [{'codec_type': 'audio'}]}))
        with patch('resolver.subprocess.run', side_effect=run):
            with ThreadPoolExecutor(max_workers=5) as pool:
                values = list(pool.map(lambda n: inspect_audio('test', {} if n % 2 else None), range(8)))
        self.assertEqual(count['calls'], 8)
        self.assertEqual(count['maximum'], 1)
        self.assertEqual(len(values), 8)

    def test_playback_inspection_precedes_queued_discovery(self):
        inspector = AudioInspector()
        order = []
        active = threading.Event()
        release = threading.Event()
        def first():
            with inspector.slot(False):
                active.set()
                release.wait(2)
        def queued(playback):
            with inspector.slot(playback):
                order.append(playback)
        with ThreadPoolExecutor(max_workers=3) as pool:
            running = pool.submit(first)
            self.assertTrue(active.wait(1))
            background = pool.submit(queued, False)
            player = pool.submit(queued, True)
            deadline = time.monotonic() + 1
            while inspector.players == 0 and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertEqual(inspector.players, 1)
            release.set()
            for future in (running, background, player):
                future.result(2)
        self.assertEqual(order, [True, False])
