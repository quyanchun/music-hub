"""Periodic cache expiry, safe catalog retention and SQLite space reclamation."""
import json
import shutil
import threading
import time

from storage import MAX_IMAGES, MIN_FREE, sweep


class Maintenance:
    def __init__(self, catalog, media, metadata):
        self.catalog, self.media, self.metadata = catalog, media, metadata
        self.lock = threading.Lock()

    def loop(self, stop):
        while not stop.is_set():
            try:
                self.run()
            except Exception as error:
                print(json.dumps({'event': 'maintenance_error', 'type': type(error).__name__}), flush=True)
            stop.wait(1800)

    def run(self):
        if not self.lock.acquire(blocking=False):
            return {}
        try:
            with self.media.guard:
                audio = self.media.cleanup()
            with self.metadata.guard:
                images = sweep(self.metadata.folder, MAX_IMAGES, 86400,
                    lambda path: bool((lock := self.metadata.locks.get(path.name.split('.')[0])) and lock.locked()))
            now = time.time()
            with self.catalog.connect() as db:
                # Reserve the writer before reading; WAL snapshots cannot be
                # upgraded after a concurrent chart/search commit.
                db.execute('BEGIN IMMEDIATE')
                tables = {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                db.execute('CREATE TEMP TABLE keep_songs(id TEXT PRIMARY KEY)')
                for row in db.execute('SELECT members FROM boards'):
                    db.executemany('INSERT OR IGNORE INTO keep_songs VALUES (?)', ((key,) for key in json.loads(row['members'])))
                db.execute('INSERT OR IGNORE INTO keep_songs SELECT id FROM annotations WHERE starred IS NOT NULL OR rating>0 OR play_count>0')
                if 'discoveries' in tables:
                    db.execute('DELETE FROM discoveries WHERE added<? OR id NOT IN (SELECT id FROM songs)', (now - 30 * 86400,))
                    db.execute('INSERT OR IGNORE INTO keep_songs SELECT id FROM discoveries')
                # Only unused, reconstructible records expire. Favorites, ratings,
                # played music and the current charts/discovery pool are protected.
                deleted = db.execute('DELETE FROM songs WHERE touched<? AND id NOT IN (SELECT id FROM keep_songs)', (now - 30 * 86400,)).rowcount
                db.execute('DELETE FROM annotations WHERE id NOT IN (SELECT id FROM songs)')
                if 'metadata' in tables:
                    db.execute("DELETE FROM metadata WHERE id NOT IN (SELECT id FROM songs) OR checked<? OR (checked<? AND COALESCE(json_extract(data,'$.lrc'),'')='' AND COALESCE(json_extract(data,'$.cover_url'),'')='')", (now - 86400, now - 600))
                if 'searches' in tables:
                    db.execute('DELETE FROM searches WHERE expires<?', (now,))
                if 'recommendation_history' in tables:
                    db.execute('DELETE FROM recommendation_history WHERE shown<?', (now - 30 * 86400,))
                if 'discovery_seeds' in tables:
                    db.execute('DELETE FROM discovery_seeds WHERE checked<?', (now - 30 * 86400,))
            with self.catalog.connect() as db:
                mode = db.execute('PRAGMA auto_vacuum').fetchone()[0]
                if mode == 2:
                    db.execute('PRAGMA incremental_vacuum(256)').fetchall()
                else:
                    pages = db.execute('PRAGMA page_count').fetchone()[0]
                    free = db.execute('PRAGMA freelist_count').fetchone()[0]
                    if free > 256 and free > pages / 4 and shutil.disk_usage(self.catalog.folder).free > self.catalog.path.stat().st_size * 2 + MIN_FREE:
                        db.execute('VACUUM')
            with self.catalog.connect() as db:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            result = {'event': 'maintenance', 'audio': audio, 'images': images,
                      'expired_songs': deleted, 'database_bytes': self.catalog.path.stat().st_size,
                      'free_bytes': shutil.disk_usage(self.catalog.folder).free}
            print(json.dumps(result), flush=True)
            return result
        finally:
            self.lock.release()
