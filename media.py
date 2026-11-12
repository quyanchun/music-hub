"""One streaming downloader, SQLite-backed persistent audio and a shared disk budget."""
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import weakref

import requests

from library import artist_names, normalized, song_entry
from memory_budget import BackgroundPaused, background_allowed
from resolver import inspect_audio, resolve, seconds, blocked
from storage import require_space

MAX_FILE = 40 * 1024 * 1024
MAX_CACHE = 20_000_000_000
AUDIO_SUFFIXES = {'.mp3', '.aac', '.m4a', '.ogg', '.flac', '.wav', '.ape'}
CACHE_PLAYLIST = 'cached-songs'


def recording_key(record):
    artist = record.get('singer') or record.get('artist') or '未知歌手'
    names = sorted(artist_names(artist))
    return hashlib.sha256((normalized(record['title']) + '\0' + '\0'.join(names)).encode()).hexdigest()


def cache_day():
    # The next daily batch starts at 03:00 China time, independent of host TZ.
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return (now - datetime.timedelta(hours=3)).strftime('%Y-%m-%d')


class CachedSongs(list):
    """A replayable, disk-backed response; JSON iterencode never collects the library."""
    def __init__(self, media):
        self.media = media

    def __len__(self):
        return self.media.summary()['count']

    def __iter__(self):
        with self.media.catalog.connect() as db:
            cursor = db.execute('SELECT id FROM audio_files WHERE verified=1 ORDER BY added DESC,id')
            for row in cursor:
                try:
                    yield self.media.entry(row['id'])
                except KeyError:
                    continue


class MediaCache:
    def __init__(self, catalog):
        self.catalog = catalog
        self.folder = catalog.folder / 'audio'
        self.folder.mkdir(exist_ok=True)
        self.guard = threading.RLock()
        self.locks = weakref.WeakValueDictionary()
        self.pins = {}
        self.slots = threading.BoundedSemaphore(1)
        self.players = 0
        self.next_background = 0
        self.reserved = 0
        self.limit = int(os.environ.get('MUSICHUB_AUDIO_CACHE_BYTES', MAX_CACHE))
        self.scan = None
        with catalog.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS audio_files (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL, duration REAL NOT NULL,
                    path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, digest TEXT NOT NULL,
                    added REAL NOT NULL, verified INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS audio_identity ON audio_files(identity,duration);
                CREATE INDEX IF NOT EXISTS audio_added ON audio_files(added,id);
                CREATE INDEX IF NOT EXISTS audio_digest ON audio_files(digest);
                CREATE TABLE IF NOT EXISTS audio_aliases (song_id TEXT PRIMARY KEY,file_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS audio_alias_file ON audio_aliases(file_id);
                CREATE TABLE IF NOT EXISTS cache_attempts (song_id TEXT PRIMARY KEY,retry_at REAL NOT NULL,failures INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS daily_tracks (day TEXT NOT NULL,file_id TEXT NOT NULL,PRIMARY KEY(day,file_id));
                CREATE INDEX IF NOT EXISTS daily_file ON daily_tracks(file_id,day);
            ''')

        catalog.has_audio = True

    def summary(self):
        with self.catalog.connect() as db:
            row = db.execute('SELECT count(*) AS count,coalesce(sum(size),0) AS bytes FROM audio_files WHERE verified=1').fetchone()
        return dict(row)

    def forget(self, file_id, cooldown=False):
        with self.catalog.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if cooldown:
                db.execute('INSERT OR REPLACE INTO cache_attempts SELECT song_id,?,0 FROM audio_aliases WHERE file_id=?',
                           (time.time() + 7 * 86400, file_id))
            db.execute('DELETE FROM audio_aliases WHERE file_id=?', (file_id,))
            db.execute('DELETE FROM audio_files WHERE id=?', (file_id,))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('cache_changed',?)", (json.dumps(time.time()),))

    def path(self, row):
        path = self.folder / row['path']
        if path.parent != self.folder or path.suffix not in AUDIO_SUFFIXES or path.is_symlink():
            raise KeyError(row['id'])
        try:
            if path.is_file() and path.stat().st_size == row['size'] and row['size'] > 0:
                return path
        except OSError:
            pass
        self.forget(row['id'])
        raise KeyError(row['id'])

    def cached(self, song_id, verified=False):
        with self.catalog.connect() as db:
            row = db.execute('SELECT f.* FROM audio_aliases a JOIN audio_files f ON f.id=a.file_id WHERE a.song_id=?', (song_id,)).fetchone()
        if row and (not verified or row['verified']):
            try:
                return row, self.path(row)
            except KeyError:
                pass
        return None

    def equivalent(self, record):
        with self.catalog.connect() as db:
            rows = db.execute('SELECT * FROM audio_files WHERE identity=? AND abs(duration-?)<=3 AND verified=1 LIMIT 8',
                              (recording_key(record), seconds(record)))
            for row in rows:
                try:
                    return row, self.path(row)
                except KeyError:
                    continue
        return None

    def bind(self, song_id, file_id):
        with self.guard, self.catalog.connect() as db:
            row = db.execute('SELECT path,size,duration FROM audio_files WHERE id=?',(file_id,)).fetchone()
            if row is None:
                raise KeyError(file_id)
            db.execute('INSERT OR REPLACE INTO audio_aliases VALUES (?,?)', (song_id, file_id))
            db.execute("UPDATE songs SET media=json_set(media,'$.suffix',?,'$.size',?,'$.duration',?) WHERE id=?",(Path(row['path']).suffix[1:],row['size'],round(row['duration']),song_id))

    def register(self, song_id, record, path, duration, digest, verified=True):
        with self.guard:
            return self._register(song_id, record, path, duration, digest, verified)

    def _register(self, song_id, record, path, duration, digest, verified):
        if verified:
            equivalent = self.equivalent(record)
            if equivalent and equivalent[0]['id'] != song_id:
                self.bind(song_id, equivalent[0]['id'])
                if path != equivalent[1]: path.unlink(missing_ok=True)
                return equivalent[1]
        with self.catalog.connect() as db:
            duplicate = db.execute('SELECT * FROM audio_files WHERE digest=? AND digest!=? LIMIT 1', (digest, '')).fetchone()
        if duplicate and duplicate['id'] != song_id:
            try:
                existing = self.path(duplicate)
                self.bind(song_id, duplicate['id'])
                if path != existing:
                    path.unlink(missing_ok=True)
                return existing
            except KeyError:
                pass
        with self.catalog.connect() as db:
            db.execute('INSERT OR REPLACE INTO audio_files VALUES (?,?,?,?,?,?,?,?)',
                       (song_id, recording_key(record), duration, path.name, path.stat().st_size, digest,
                        path.stat().st_mtime, int(verified)))
            db.execute('INSERT OR REPLACE INTO audio_aliases VALUES (?,?)', (song_id, song_id))
            if verified:
                db.execute("UPDATE songs SET media=json_set(media,'$.suffix',?,'$.size',?,'$.duration',?,'$.duration_verified',json('true')) WHERE id=?",(path.suffix[1:],path.stat().st_size,round(duration),song_id))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('cache_changed',?)", (json.dumps(time.time()),))
        return path

    def protected(self, file_id):
        lock = self.locks.get(file_id)
        if self.pins.get(file_id, 0) or (lock and lock.locked()):
            return True
        with self.catalog.connect() as db:
            active = db.execute("SELECT value FROM meta WHERE key='daily_active'").fetchone()
            active = json.loads(active['value']) if active else cache_day()
            return bool(db.execute('SELECT 1 FROM daily_tracks WHERE file_id=? AND day IN (?,?)', (file_id, cache_day(), active)).fetchone())

    def cleanup(self, reserve=0):
        with self.guard:
            with self.catalog.connect() as db, os.scandir(self.folder) as files:
                total = 0
                for item in files:
                    if item.is_symlink() or not item.is_file() or not re.fullmatch(r'[a-f0-9]{32}\.(mp3|aac|m4a|ogg|flac|wav|ape|part)',item.name): continue
                    key = Path(item.name).stem
                    stale = time.time()-item.stat().st_mtime>3600
                    known = not stale or bool(db.execute('SELECT 1 FROM songs WHERE id=?',(key,)).fetchone())
                    if stale and (item.name.endswith('.part') or not known) and not self.protected(key):
                        Path(item.path).unlink(missing_ok=True)
                        continue
                    total += item.stat().st_size
            removed = 0
            while total + self.reserved + reserve > self.limit:
                victim = None
                with self.catalog.connect() as db:
                    for row in db.execute('SELECT * FROM audio_files ORDER BY added,id'):
                        if not self.protected(row['id']):
                            victim = dict(row)
                            break
                if victim is None:
                    raise BackgroundPaused('缓存额度暂被播放或推荐占用')
                path = self.folder / victim['path']
                try:
                    actual_size = path.stat().st_size if path.is_file() and not path.is_symlink() else 0
                except FileNotFoundError:
                    actual_size = 0
                path.unlink(missing_ok=True)
                self.forget(victim['id'], cooldown=True)
                total -= actual_size; removed += 1
            return {'bytes': total, 'removed': removed}

    @contextmanager
    def open_audio(self, song_id):
        local_only = song_id.startswith('cache-')
        key = song_id.removeprefix('cache-')
        with self.guard:
            self.players += 1
            hit = self.cached(key)
            file_id = hit[0]['id'] if hit else key
            self.pins[file_id] = self.pins.get(file_id, 0) + 1
        try:
            path = self.prepare(key, local_only=local_only)
            with self.guard:
                actual = path.stem
                if actual != file_id:
                    self.pins[file_id] -= 1
                    if not self.pins[file_id]: del self.pins[file_id]
                    file_id = actual
                    self.pins[file_id] = self.pins.get(file_id, 0) + 1
            with path.open('rb') as stream:
                yield path, stream
        finally:
            with self.guard:
                self.players -= 1
                self.pins[file_id] -= 1
                if not self.pins[file_id]: del self.pins[file_id]

    def prepare(self, song_id, local_only=False, background=False):
        with self.guard:
            hit = self.cached(song_id)
            if hit:
                return hit[1]
            if local_only:
                raise KeyError('本地缓存已被移除，请刷新缓存歌单')
            lock = self.locks.setdefault(song_id, threading.Lock())
        with lock:
            hit = self.cached(song_id)
            if hit: return hit[1]
            record = self.catalog.song(song_id)
            equivalent = self.equivalent(record)
            if equivalent:
                self.bind(song_id, equivalent[0]['id'])
                return equivalent[1]
            # Legacy cache hits remain usable; the background reconciler verifies them.
            for path in self.folder.glob(song_id + '.*'):
                if path.suffix in AUDIO_SUFFIXES and not path.is_symlink() and path.stat().st_size:
                    return self.register(song_id, record, path, seconds(record), '', verified=False)
            if background and (self.players or not background_allowed()):
                raise BackgroundPaused('前台播放或内存压力，稍后继续')
            if background:
                while time.monotonic() < self.next_background:
                    if self.players or not background_allowed(): raise BackgroundPaused('后台请求让出预算')
                    time.sleep(max(0, min(.25, self.next_background-time.monotonic())))
            acquired = self.slots.acquire(blocking=False) if background else self.slots.acquire(timeout=5)
            if not acquired:
                raise BackgroundPaused('下载线程繁忙，请稍后重试')
            partial = self.folder / (song_id + '.part')
            reserved = False
            try:
                with self.guard:
                    self.cleanup(MAX_FILE)
                    require_space(self.catalog.folder, MAX_FILE + 2 * 1024 * 1024 * 1024)
                    self.reserved += MAX_FILE; reserved = True
                if background: self.next_background = time.monotonic()+5
                url, headers, suffix = resolve(record)
                if '.'+suffix not in AUDIO_SUFFIXES: raise ValueError('unsupported_audio_format')
                digest = hashlib.sha256()
                with requests.get(url, headers=headers, stream=True, timeout=(8, 25)) as response:
                    response.raise_for_status()
                    if int(response.headers.get('Content-Length') or 0) > MAX_FILE:
                        raise ValueError('音频文件超过大小限制')
                    received = 0
                    with partial.open('wb') as output:
                        for chunk in response.iter_content(65536):
                            if background and (self.players or not background_allowed()):
                                raise BackgroundPaused('后台下载让出内存或播放预算')
                            received += len(chunk)
                            if received > MAX_FILE: raise ValueError('音频文件超过大小限制')
                            digest.update(chunk); output.write(chunk)
                if background and (self.players or not background_allowed()):
                    raise BackgroundPaused('后台校验让出内存预算')
                length = int(response.headers.get('Content-Length') or 0)
                if length and received != length: raise ValueError('音频下载不完整')
                info = inspect_audio(partial, background=background)
                duration = float(info.get('format', {}).get('duration') or 0)
                if not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])) or duration <= 0:
                    raise ValueError('未取得有效音频')
                if abs(seconds(record) - duration) > max(8, seconds(record) * .03):
                    self.catalog.exclude(song_id, 'short_preview_at_playback')
                    raise ValueError('音频时长不符，未缓存')
                target = self.folder / (song_id + '.' + suffix)
                os.replace(partial, target)
                with self.guard:
                    return self.register(song_id, record, target, duration, digest.hexdigest())
            finally:
                partial.unlink(missing_ok=True)
                with self.guard:
                    if reserved: self.reserved -= MAX_FILE
                self.slots.release()

    def reconcile_one(self):
        """Bounded recovery after a crash: one file/stat/probe per scheduler tick."""
        if self.scan is None: self.scan = os.scandir(self.folder)
        try:
            item = next(self.scan)
        except StopIteration:
            self.scan.close(); self.scan = None
            # Remove missing DB entries without constructing a Python file list.
            with self.catalog.connect() as db:
                for row in db.execute('SELECT * FROM audio_files'):
                    try: self.path(row)
                    except KeyError: pass
            return False
        path = Path(item.path)
        if item.is_symlink() or not item.is_file() or not re.fullmatch(r'[a-f0-9]{32}\.(mp3|aac|m4a|ogg|flac|wav|ape|part)', item.name): return True
        if path.suffix == '.part':
            lock = self.locks.get(path.stem)
            if time.time() - item.stat().st_mtime > 3600 and not (lock and lock.locked()): path.unlink(missing_ok=True)
            return True
        hit = self.cached(path.stem, verified=True)
        if hit: return True
        if self.players or not background_allowed(): raise BackgroundPaused('等待后台校验预算')
        try:
            with self.catalog.connect() as db:
                row = db.execute('SELECT data FROM songs WHERE id=?',(path.stem,)).fetchone()
            if row is None: raise KeyError(path.stem)
            record = json.loads(row['data'])
            if blocked(record): raise ValueError('membership_or_trial')
            info = inspect_audio(path, background=True)
            duration = float(info.get('format', {}).get('duration') or 0)
            if path.stat().st_size > MAX_FILE or not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])) or abs(seconds(record)-duration)>max(8, seconds(record)*.03):
                raise ValueError('invalid_cached_audio')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(65536): digest.update(chunk)
            with self.guard: self.register(path.stem, record, path, duration, digest.hexdigest())
        except (KeyError, ValueError, subprocess.CalledProcessError):
            with self.guard:
                if not self.protected(path.stem):
                    path.unlink(missing_ok=True); self.forget(path.stem)
        return True

    def entry(self, file_id):
        hit = self.cached(file_id, verified=True)
        if not hit: raise KeyError(file_id)
        row = next(self.catalog.iter_visible([file_id]), None)
        if row is None: raise KeyError(file_id)
        song = song_entry(row)
        song.update(id='cache-' + file_id, suffix=hit[1].suffix[1:], size=hit[0]['size'], duration=round(hit[0]['duration']))
        return song

    def playlist(self, username, entries=False):
        with self.catalog.connect() as db:
            for row in db.execute('SELECT * FROM audio_files'):
                try: self.path(row)
                except KeyError: pass
        summary = self.summary()
        with self.catalog.connect() as db:
            changed = db.execute("SELECT value FROM meta WHERE key='cache_changed'").fetchone()
        changed = datetime.datetime.fromtimestamp(float(changed['value']) if changed else time.time(),datetime.timezone.utc).isoformat(timespec='seconds')
        result = {'id': CACHE_PLAYLIST, 'name': '缓存歌曲歌单', 'owner': username, 'public': False,
                  'songCount': summary['count'], 'created': changed,
                  'changed': changed, 'comment': '已下载并校验完整音频，播放仅访问服务器本地文件'}
        if entries: result['entry'] = CachedSongs(self)
        return result
