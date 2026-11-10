"""Retention, active-reader safety, cache budgets and disk-full protection."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
from contextlib import contextmanager
from unittest.mock import patch

from catalog import Catalog
from storage import MAX_DATABASE, MIN_FREE, sweep
from test_service import HTTPFixture, insert, record


class StorageTests(HTTPFixture):
    def file(self, folder, n, suffix, size=100, age=0):
        path = folder / (format(n, '032x') + suffix)
        path.write_bytes(b'x' * size)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def test_expired_and_orphan_files_are_cleaned_without_new_playback(self):
        old = self.file(self.app.media.folder, 100, '.mp3', age=90000)
        orphan = self.file(self.app.media.folder, 101, '.part', age=7200)
        fresh = self.file(self.app.media.folder, 102, '.part')
        unknown = self.app.media.folder / 'user-note.txt'
        unknown.write_text('keep')
        self.app.maintenance.run()
        self.assertFalse(old.exists())
        self.assertFalse(orphan.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unknown.exists())

    def test_active_audio_reader_and_downloader_are_not_deleted(self):
        key = self.ids[0]
        with self.app.media.open_audio(key) as (path, stream):
            os.utime(path, (time.time() - 90000,) * 2)
            self.app.maintenance.run()
            self.assertTrue(path.exists())
            self.assertEqual(stream.read(3), b'ID3')
        self.app.maintenance.run()
        self.assertFalse(path.exists())
        active = self.file(self.app.media.folder, 110, '.part', age=7200)
        lock = threading.Lock()
        self.app.media.locks[active.stem] = lock
        with lock:
            with self.app.media.guard:
                self.app.media.cleanup()
            self.assertTrue(active.exists())
        with self.app.media.guard:
            self.app.media.cleanup()
        self.assertFalse(active.exists())

    def test_size_budget_evicts_oldest_and_ignores_symlinks(self):
        folder = self.app.metadata.folder
        paths = [self.file(folder, n, '.image', size=80, age=30 - n) for n in range(3)]
        outside = Path(self.state.name) / 'outside.image'
        outside.write_bytes(b'private')
        link = folder / (format(99, '032x') + '.image')
        link.symlink_to(outside)
        report = sweep(folder, 100, 86400)
        self.assertEqual(report['bytes'], 80)
        self.assertEqual(report['removed'], 2)
        self.assertTrue(paths[-1].exists())
        self.assertEqual(outside.read_bytes(), b'private')
        self.assertTrue(link.is_symlink())

    def test_cover_writes_enforce_budget_immediately(self):
        key = self.ids[0]
        for n in range(10):
            self.file(self.app.metadata.folder, n + 1000, '.image', size=128, age=100)
        with self.app.catalog.connect() as db:
            db.execute('INSERT INTO metadata VALUES (?,?,?)', (key, json.dumps({'cover_url': 'https://music.126.net/example.png'}), time.time()))
        raw = b'\x89PNG\r\n\x1a\n' + b'a' * 120
        with patch('metadata.Fetch.raw', return_value=raw), patch('metadata.MAX_IMAGES', 256):
            self.assertEqual(self.app.metadata.cover(key)[0], raw)
        self.assertLessEqual(sum(p.stat().st_size for p in self.app.metadata.folder.glob('*.image')), 256)

    def test_retention_preserves_charts_discovery_favorites_ratings_and_plays(self):
        keys = {name: insert(self.app.catalog, record(name)) for name in ('unused', 'starred', 'rated', 'played', 'discovered', 'read')}
        with self.app.catalog.connect() as db:
            db.execute('UPDATE songs SET touched=?', (time.time() - 40 * 86400,))
            db.execute('INSERT INTO discoveries VALUES (?,?)', (keys['discovered'], time.time()))
        self.app.catalog.annotate([keys['starred']], 'star')
        self.app.catalog.annotate([keys['rated']], 'setRating', 4)
        self.app.catalog.annotate([keys['played']], 'scrobble')
        self.app.catalog.song(keys['read'])
        self.app.maintenance.run()
        with self.app.catalog.connect() as db:
            remaining = {r['id'] for r in db.execute('SELECT id FROM songs')}
        self.assertNotIn(keys['unused'], remaining)
        self.assertTrue(set(self.ids) <= remaining)
        self.assertTrue(set(keys.values()) - {keys['unused']} <= remaining)

    def test_expired_sql_caches_are_removed(self):
        with self.app.catalog.connect() as db:
            db.execute('INSERT INTO metadata VALUES (?,?,?)', (self.ids[0], '{"lrc":"old"}', time.time() - 90000))
            db.execute('INSERT INTO searches VALUES (?,?,?,?)', ('expired', '[]', time.time() - 1, '{}'))
            db.execute('INSERT INTO recommendation_history VALUES (?,?)', ('old', time.time() - 31 * 86400))
            db.execute('INSERT INTO discovery_seeds VALUES (?,?)', ('old', time.time() - 31 * 86400))
        self.app.maintenance.run()
        with self.app.catalog.connect() as db:
            for table in ('metadata', 'searches', 'recommendation_history', 'discovery_seeds'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone()[0], 2)
            pages = db.execute('PRAGMA max_page_count').fetchone()[0]
            self.assertEqual(pages * db.execute('PRAGMA page_size').fetchone()[0], MAX_DATABASE)

    def test_low_disk_blocks_new_downloads_but_cached_audio_still_plays(self):
        key = insert(self.app.catalog, record('new-on-full-disk'))
        with patch('storage.shutil.disk_usage', return_value=SimpleNamespace(free=MIN_FREE - 1)), patch('media.resolve') as resolver:
            self.assertTrue(self.app.media.prepare(self.ids[0]).exists())
            with self.assertRaises(ValueError):
                self.app.media.prepare(key)
            resolver.assert_not_called()
        self.assertEqual(list(self.app.media.folder.glob('*.part')), [])

    def test_periodic_maintenance_runs_without_an_api_request(self):
        stop = threading.Event()
        with patch.object(self.app.maintenance, 'run', side_effect=stop.set) as run:
            self.app.maintenance.loop(stop)
        run.assert_called_once()

    def test_maintenance_serializes_with_background_catalog_writer(self):
        start, committed = threading.Event(), threading.Event()
        connect = self.app.catalog.connect
        errors = []

        def writer():
            start.wait(2)
            try:
                with connect() as db:
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('concurrent', '{}')")
            except Exception as error:
                errors.append(error)
            finally:
                committed.set()

        def trace(sql):
            if sql.startswith('DELETE FROM discoveries'):
                start.set()
                committed.wait(.15)

        @contextmanager
        def traced_connect():
            with connect() as db:
                db.set_trace_callback(trace)
                yield db

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with patch.object(self.app.catalog, 'connect', traced_connect):
                self.app.maintenance.run()
        finally:
            start.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


class MigrationTests(__import__('unittest').TestCase):
    def test_sqlite_hard_budget_rejects_growth(self):
        with tempfile.TemporaryDirectory() as folder, patch('catalog.MAX_DATABASE', 128 * 1024):
            catalog = Catalog(folder)
            with self.assertRaises(sqlite3.DatabaseError):
                with catalog.connect() as db:
                    db.execute('INSERT INTO meta VALUES (?,?)', ('too-large', 'x' * (256 * 1024)))
            self.assertLessEqual(catalog.path.stat().st_size, 128 * 1024)

    def test_existing_catalog_and_stable_ids_survive_usage_column_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'catalog.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE songs(id TEXT PRIMARY KEY,data TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',media TEXT NOT NULL DEFAULT '{}',reason TEXT NOT NULL DEFAULT '',checked TEXT)")
                db.execute('INSERT INTO songs(id,data,status) VALUES (?,?,?)', ('existing', json.dumps(record()), 'accepted'))
            catalog = Catalog(folder)
            self.assertEqual(catalog.song('existing')['title'], record()['title'])
            with catalog.connect() as db:
                self.assertGreater(db.execute('SELECT touched FROM songs').fetchone()[0], 0)
