"""A separate, resumable chart catalog. The music station database is never opened."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

from resolver import NAMES, blocked, probe
from charts import BOARDS, get_chart
from storage import MAX_DATABASE, require_space


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def identifier(source, value):
    return hashlib.sha256((source + ':' + str(value)).encode()).hexdigest()[:32]


class Catalog:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'catalog.sqlite3'
        self.scan_lock = threading.Lock()
        self.has_audio = False
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        with self.connect() as db:
            if fresh:
                db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            self.has_audio = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='audio_files'").fetchone())
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS songs (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    media TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL DEFAULT '', checked TEXT,
                    touched REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS boards (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
                    total INTEGER NOT NULL, members TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS annotations (
                    id TEXT PRIMARY KEY, starred TEXT, rating INTEGER DEFAULT 0,
                    play_count INTEGER DEFAULT 0, last_played TEXT);
            ''')
            if 'touched' not in {r['name'] for r in db.execute('PRAGMA table_info(songs)')}:
                db.execute('ALTER TABLE songs ADD COLUMN touched REAL NOT NULL DEFAULT 0')
            db.execute('UPDATE songs SET touched=? WHERE touched=0', (time.time(),))
            db.executescript('''
                CREATE INDEX IF NOT EXISTS songs_touched ON songs(touched);
                CREATE TRIGGER IF NOT EXISTS songs_touch_insert AFTER INSERT ON songs
                WHEN NEW.touched=0 BEGIN
                    UPDATE songs SET touched=CAST(strftime('%s','now') AS REAL) WHERE id=NEW.id;
                END;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20, cached_statements=16)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA cache_size=-128')
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA mmap_size=0')
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count=' + str(MAX_DATABASE // page_size))
            db.execute('PRAGMA journal_size_limit=8388608')
            db.execute('PRAGMA wal_autocheckpoint=256')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def status(self):
        with self.connect() as db:
            counts = {r['status']: r['count'] for r in db.execute('SELECT status,count(*) AS count FROM songs GROUP BY status')}
            boards = [dict(r) for r in db.execute('SELECT id,source,name,total,updated FROM boards ORDER BY id')]
            meta = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM meta')}
        return {'counts': counts, 'boards': boards, **meta}

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def visible(self):
        return list(self.iter_visible()), self.boards()

    def boards(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM boards ORDER BY id')]

    def visibility(self, alias='songs'):
        condition = f"{alias}.status='accepted'"
        if self.has_audio:
            condition += f' OR EXISTS(SELECT 1 FROM audio_aliases ca JOIN audio_files cf ON cf.id=ca.file_id WHERE ca.song_id={alias}.id)'
        return '(' + condition + ')'

    @staticmethod
    def view_row(row):
        return {'key': row['id'], 'record': json.loads(row['data']), 'media': json.loads(row['media']), 'checked': row['checked'],
                **{k: row[k] for k in ('starred', 'rating', 'play_count', 'last_played')}}

    def iter_visible(self, keys=None):
        if keys is not None and not keys:
            return
        with self.connect() as db:
            query = "SELECT s.id,s.data,s.media,s.checked,a.starred,a.rating,a.play_count,a.last_played FROM songs s LEFT JOIN annotations a USING(id) WHERE " + self.visibility("s")
            values = [] if keys is None else list(keys)
            if keys is not None:
                query += ' AND s.id IN (' + ','.join('?' for _ in values) + ')'
            for row in db.execute(query, values):
                yield self.view_row(row)

    def playlist_rows(self, key):
        with self.connect() as db:
            query = "SELECT s.id,s.data,s.media,s.checked,a.starred,a.rating,a.play_count,a.last_played FROM boards b,json_each(b.members) m JOIN songs s ON s.id=m.value LEFT JOIN annotations a ON a.id=s.id WHERE b.id=? AND " + self.visibility('s') + " ORDER BY cast(m.key AS INTEGER)"
            for row in db.execute(query, (key,)):
                yield self.view_row(row)

    def playlist_headers(self, username):
        with self.connect() as db:
            for board in db.execute('SELECT id,name,total,updated FROM boards ORDER BY id'):
                row = db.execute("SELECT count(*) AS count,coalesce(sum(json_extract(s.media,'$.duration')),0) AS duration FROM boards b,json_each(b.members) m JOIN songs s ON s.id=m.value WHERE b.id=? AND " + self.visibility('s'), (board['id'],)).fetchone()
                if board['id'].startswith('pl-') and not row['count']: continue
                yield {'id':board['id'],'name':board['name'],'owner':username,'public':False,'songCount':row['count'],
                       'duration':int(row['duration']),'created':board['updated'],'changed':board['updated'],
                       'comment':f"公开可播曲目；原单 {board['total']} 首"}

    def cover_keys(self, key):
        from resolver import NAMES
        if key.startswith(('al-', 'ar-', 'pl-')):
            key = key[3:]
        with self.connect() as db:
            if db.execute("SELECT 1 FROM songs WHERE id=? AND status='accepted'", (key,)).fetchone():
                return [key]
            board = db.execute('SELECT members FROM boards WHERE id=?', (key,)).fetchone()
            if board:
                keys = []
                for member in json.loads(board['members']):
                    if db.execute("SELECT 1 FROM songs WHERE id=? AND status='accepted'", (member,)).fetchone():
                        keys.append(member)
                        if len(keys) == 32:
                            break
                return keys
            keys = []
            for row in db.execute("SELECT id,data FROM songs WHERE status='accepted'"):
                record = json.loads(row['data'])
                source = record['source']
                artist = record.get('singer') or '未知歌手'
                album = record.get('album') or NAMES[source] + ' · 榜单精选'
                if key in ('ar' + identifier(source, artist), 'al' + identifier(source, artist + '\0' + album)):
                    keys.append(row['id'])
                    if len(keys) == 32:
                        break
            return keys

    def song(self, song_id):
        song_id = song_id.removeprefix('cache-')
        with self.connect() as db:
            row = db.execute("SELECT * FROM songs WHERE id=? AND " + self.visibility(), (song_id,)).fetchone()
        if not row:
            raise KeyError(song_id)
        self.touch(song_id)
        return json.loads(row['data'])

    def touch(self, song_id):
        now = time.time()
        with self.connect() as db:
            db.execute('UPDATE songs SET touched=? WHERE id=? AND touched<?', (now, song_id, now - 3600))

    def exclude(self, song_id, reason):
        with self.connect() as db:
            db.execute("UPDATE songs SET status='unavailable',reason=?,checked=NULL WHERE id=?", (reason, song_id))

    def annotate(self, ids, operation, value=None):
        with self.connect() as db:
            for song_id in ids:
                if not db.execute("SELECT 1 FROM songs WHERE id=? AND " + self.visibility(), (song_id,)).fetchone():
                    raise KeyError(song_id)
                db.execute('INSERT OR IGNORE INTO annotations(id) VALUES (?)', (song_id,))
                if operation == 'star':
                    db.execute('UPDATE annotations SET starred=? WHERE id=?', (utc(), song_id))
                elif operation == 'unstar':
                    db.execute('UPDATE annotations SET starred=NULL WHERE id=?', (song_id,))
                elif operation == 'setRating':
                    db.execute('UPDATE annotations SET rating=? WHERE id=?', (value, song_id))
                elif operation == 'scrobble':
                    db.execute('UPDATE annotations SET play_count=play_count+1,last_played=? WHERE id=?', (utc(), song_id))

    def scan(self, workers=4):
        if not self.scan_lock.acquire(blocking=False):
            return
        scan_file = (self.folder / 'scan.lock').open('a')
        try:
            fcntl.flock(scan_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            scan_file.close()
            self.scan_lock.release()
            return
        started = utc()
        self.set_meta('scan', {'running': True, 'started': started, 'phase': 'charts'})
        errors = {}
        try:
            def collect(source, chart):
                members, page, total, name = [], 1, 0, ''
                while True:
                    last_error = None
                    for attempt in range(2):
                        try:
                            result = get_chart(source, chart, page)
                            break
                        except Exception as error:
                            last_error = error
                            time.sleep(1)
                    else:
                        raise ValueError(f'page {page}: {type(last_error).__name__}')
                    name, total = result['name'], result['total']
                    require_space(self.folder)
                    with self.connect() as db:
                        for song in result['songs']:
                            key = identifier(source, song['id'])
                            members.append(key)
                            if blocked(song):
                                db.execute("INSERT INTO songs(id,data,status,reason) VALUES (?,?,'rejected','membership_or_trial') ON CONFLICT(id) DO UPDATE SET data=excluded.data,status='rejected',reason='membership_or_trial'", (key, json.dumps(song, ensure_ascii=False)))
                            else:
                                db.execute("INSERT INTO songs(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,checked=CASE WHEN songs.status='rejected' THEN NULL ELSE songs.checked END,status=CASE WHEN songs.status='rejected' THEN 'pending' ELSE songs.status END", (key, json.dumps(song, ensure_ascii=False)))
                    if not result['has_more']:
                        break
                    page += 1
                    if page > 100:
                        raise ValueError('unexpected_chart_size')
                    time.sleep(.15)
                board_id = source + '-' + chart
                with self.connect() as db:
                    db.execute('INSERT OR REPLACE INTO boards VALUES (?,?,?,?,?,?)',
                               (board_id, source, NAMES[source] + ' · ' + name, total, json.dumps(members), utc()))
                print(f'chart {board_id}: {len(members)}/{total}', flush=True)

            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(collect, source, chart): source + '-' + chart for source in BOARDS for chart in BOARDS[source]}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as error:
                        errors[futures[future]] = str(error)
                        print('chart error', futures[future], type(error).__name__, flush=True)
            self.set_meta('scan', {'running': True, 'started': started, 'phase': 'availability', 'chart_errors': errors})
            cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=12)).isoformat(timespec='seconds').replace('+00:00', 'Z')

            def check(candidate):
                key, song = candidate
                try:
                    media = probe(song)
                    state, reason = 'accepted', ''
                except Exception as error:
                    media = {}
                    state = 'unavailable'
                    reason = str(error)[:160] if isinstance(error, ValueError) else type(error).__name__
                with self.connect() as db:
                    db.execute('UPDATE songs SET status=?,media=?,reason=?,checked=? WHERE id=?',
                               (state, json.dumps(media), reason, utc(), key))

            with ThreadPoolExecutor(max_workers=workers) as pool:
                last, number = '', 0
                while True:
                    with self.connect() as db:
                        candidates = [(r['id'], json.loads(r['data'])) for r in db.execute(
                            "SELECT id,data FROM songs WHERE id>? AND status!='rejected' AND (checked IS NULL OR checked<?) ORDER BY id LIMIT 64", (last, cutoff))]
                    if not candidates:
                        break
                    last = candidates[-1][0]
                    for _ in pool.map(check, candidates):
                        number += 1
                        if number % 50 == 0:
                            print(f'availability {number}', flush=True)
            self.set_meta('scan', {'running': False, 'started': started, 'finished': utc(), 'phase': 'done', 'chart_errors': errors})
        except Exception as error:
            self.set_meta('scan', {'running': False, 'started': started, 'finished': utc(), 'phase': 'error', 'error': type(error).__name__, 'chart_errors': errors})
            raise
        finally:
            scan_file.close()
            self.scan_lock.release()

    def start_scan(self):
        threading.Thread(target=self.scan, daemon=True, name='chart-sync').start()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default=str(Path(__file__).resolve().parent / 'data'))
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    catalog = Catalog(args.data)
    catalog.scan(args.workers)
    print(json.dumps(catalog.status(), ensure_ascii=False, indent=2))
