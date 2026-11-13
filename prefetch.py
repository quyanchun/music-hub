"""One resumable worker. Queues, daily batches, retries and history live in SQLite."""
import datetime
import json
import os
import threading
import time

from library import FOLDERS
from media import cache_day, recording_key
from memory_budget import BackgroundPaused, background_allowed, memory_snapshot
from resolver import NAMES, seconds


class Prefetch:
    def __init__(self, catalog, media, search):
        self.catalog, self.media, self.search = catalog, media, search
        self.interval = max(5, float(os.environ.get('MUSICHUB_PREFETCH_INTERVAL', '10')))
        self.daily_size = 50
        self.download_interval = max(180, float(os.environ.get('MUSICHUB_CACHE_INTERVAL', '180')))
        self.lock = threading.Lock()
        self.next_reconcile = 0

    def fill_daily(self, day):
        cutoff = (datetime.date.fromisoformat(day) - datetime.timedelta(days=7)).isoformat()
        with self.catalog.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('SELECT count(*) FROM daily_tracks d JOIN audio_files f ON f.id=d.file_id WHERE day=? AND f.verified=1', (day,)).fetchone()[0]
            if count < self.daily_size:
                rows = db.execute('''SELECT f.id FROM audio_files f WHERE verified=1
                    AND NOT EXISTS(SELECT 1 FROM daily_tracks d WHERE d.file_id=f.id AND d.day=?)
                    ORDER BY EXISTS(SELECT 1 FROM daily_tracks d WHERE d.file_id=f.id AND d.day>=?),
                    random() LIMIT ?''', (day, cutoff, self.daily_size-count)).fetchall()
                db.executemany('INSERT OR IGNORE INTO daily_tracks VALUES (?,?)', ((day, r['id']) for r in rows))
                count += len(rows)
            if count == self.daily_size:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('daily_active',?)", (json.dumps(day),))
            db.execute('DELETE FROM daily_tracks WHERE day<?', ((datetime.date.fromisoformat(day)-datetime.timedelta(days=30)).isoformat(),))
        return count

    def daily(self, size, params):
        day = cache_day()
        self.fill_daily(day)
        with self.catalog.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='daily_active'").fetchone()
            active = json.loads(row['value']) if row else day
            ids = [r['file_id'] for r in db.execute('SELECT d.file_id FROM daily_tracks d JOIN audio_files f ON f.id=d.file_id WHERE d.day=? AND f.verified=1 ORDER BY d.file_id LIMIT 50', (active,))]
        songs = []
        source = FOLDERS.get(str(params.get('musicFolderId')))
        for key in ids:
            try:
                song = self.media.entry(key)
            except KeyError:
                continue
            if (not source or song['genre'] == NAMES[source]) and (not params.get('genre') or song['genre']==params['genre']):
                songs.append(song)
            if len(songs) >= size: break
        return songs[:size]

    def retry(self, key, error=None):
        with self.catalog.connect() as db:
            row = db.execute('SELECT failures FROM cache_attempts WHERE song_id=?', (key,)).fetchone()
            failures = (row['failures'] if row else 0) + 1
            source = db.execute("SELECT json_extract(data,'$.source') FROM songs WHERE id=?",(key,)).fetchone()
            db.execute('INSERT OR REPLACE INTO cache_attempts VALUES (?,?,?)',
                       (key, time.time()+min(6*3600, 1800*2**min(failures-1,4)), failures))
        if source: self.source_result(source[0], error or ValueError('download_failed'))

    def available_sources(self):
        with self.catalog.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='source_backoff'").fetchone()
        cooldown = json.loads(row['value']) if row else {}
        return [source for source in NAMES if cooldown.get(source,0)<=time.time()]

    def source_result(self, source, error=None):
        now = time.time()
        with self.catalog.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM meta WHERE key='download_source_health'").fetchone()
            health = json.loads(row['value']) if row else {}
            row = db.execute("SELECT value FROM meta WHERE key='source_backoff'").fetchone()
            cooldown = json.loads(row['value']) if row else {}
            state = health.get(source, {})
            if state.get('blocked_until') and state['blocked_until']<=now: state = {}
            if error is None:
                state = {'failures':0,'blocked_until':0,'last_error':None}
                cooldown.pop(source,None)
            else:
                status = getattr(getattr(error,'response',None),'status_code',None)
                limited = status in (403,429) or '频繁' in str(error)
                state.update(failures=state.get('failures',0)+1,last_error='rate_limited' if limited else type(error).__name__)
                if limited: cooldown[source] = now+1800
                if state['failures']>=3:
                    china = datetime.timezone(datetime.timedelta(hours=8))
                    tomorrow = datetime.datetime.fromtimestamp(now,china).date()+datetime.timedelta(days=1)
                    state['blocked_until'] = datetime.datetime.combine(tomorrow,datetime.time(),china).timestamp()
                    cooldown[source] = max(cooldown.get(source,0),state['blocked_until'])
            state['updated'] = now
            health[source] = state
            db.execute("INSERT OR REPLACE INTO meta VALUES ('download_source_health',?)",(json.dumps(health),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('source_backoff',?)",(json.dumps(cooldown),))

    def fetch(self, key, record):
        if record['source'] not in self.available_sources(): raise BackgroundPaused('自动下载音源已暂停')
        already_cached = self.media.cached(key, verified=True) or self.media.equivalent(record)
        try:
            path = self.media.prepare(key, background=True)
        except (PermissionError,FileNotFoundError) as error:
            raise BackgroundPaused('等待本地缓存目录恢复') from error
        except ValueError as error:
            if '磁盘剩余空间不足' in str(error): raise BackgroundPaused('等待磁盘空间') from error
            raise
        hit = self.media.cached(path.stem, verified=True)
        if not hit: raise BackgroundPaused('等待已有文件完整校验')
        if not already_cached: self.source_result(record['source'])
        return hit[0]['id']

    def download(self, key):
        original = self.catalog.song(key)
        try:
            return self.fetch(key,original)
        except BackgroundPaused:
            raise
        except Exception as error:
            self.retry(key,error)
        # Prefer another already-known platform before asking the remote search APIs.
        with self.catalog.connect() as db:
            alternatives = [r['id'] for r in db.execute("SELECT id FROM songs WHERE status='accepted' AND id!=? AND json_extract(data,'$.title')=? LIMIT 24", (key, original['title']))]
        for remote in (False, True):
            if remote:
                if self.media.players or not background_allowed(): raise BackgroundPaused('等待搜索预算')
                sources = self.available_sources()
                if not sources: return None
                alternatives = self.search.search((original['title']+' '+original.get('singer','')).strip(),sources=sources)[:64]
            for alternate in alternatives:
                if alternate == key: continue
                try:
                    record = self.catalog.song(alternate)
                    if recording_key(record)!=recording_key(original) or abs(seconds(record)-seconds(original))>3: continue
                    with self.catalog.connect() as db:
                        retry = db.execute('SELECT 1 FROM cache_attempts WHERE song_id=? AND retry_at>?', (alternate,time.time())).fetchone()
                        cooldown = db.execute("SELECT coalesce(json_extract(value,?),0) FROM meta WHERE key='source_backoff'",('$.'+record['source'],)).fetchone()
                    if retry or (cooldown and cooldown[0]>time.time()): continue
                    file_id = self.fetch(alternate,record)
                    self.media.bind(key, file_id)
                    return file_id
                except BackgroundPaused: raise
                except Exception as error: self.retry(alternate,error)
        return None

    def step(self):
        if self.media.players or not background_allowed(): raise BackgroundPaused('前台优先或内存压力')
        day, now = cache_day(), time.time()
        if now >= self.next_reconcile:
            if not self.media.reconcile_one():
                self.next_reconcile = now+1800
        with self.catalog.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='prefetch'").fetchone()
            state = json.loads(row['value']) if row else {}
        state['day'] = day
        state.pop('extra', None)
        state.pop('ready', None)
        hour = int(now//3600)
        if state.get('hour')!=hour: state.update(hour=hour,attempts=0,downloaded=0)
        if now < state.get('next_download',0):
            return
        with self.catalog.connect() as db:
            # A disk-backed random sort, not an in-memory all-song queue.
            db.execute('DELETE FROM cache_attempts WHERE retry_at<?', (now-7*86400,))
            row = db.execute('''SELECT s.id FROM songs s
                LEFT JOIN audio_aliases a ON a.song_id=s.id
                LEFT JOIN cache_attempts c ON c.song_id=s.id
                WHERE s.status='accepted' AND a.song_id IS NULL AND coalesce(c.retry_at,0)<=?
                AND coalesce(json_extract((SELECT value FROM meta WHERE key='source_backoff'),'$.'||json_extract(s.data,'$.source')),0)<=?
                ORDER BY CASE WHEN s.id IN
                    (SELECT m.value FROM boards b,json_each(b.members) m) THEN 0 ELSE 1 END,
                    random() LIMIT 1''', (now, now)).fetchone()
        if row is None: return
        state['attempts'] += 1
        state['next_download'] = now+self.download_interval
        state['interval'] = self.download_interval
        self.catalog.set_meta('prefetch',state)
        file_id = self.download(row['id'])
        if file_id:
            with self.catalog.connect() as db:
                added = db.execute('SELECT added FROM audio_files WHERE id=?',(file_id,)).fetchone()
            # Count new audio files, not aliases or recovered old files.
            if added and added['added'] >= now:
                completed_hour = int(time.time()//3600)
                if completed_hour != state['hour']:
                    state.update(hour=completed_hour,attempts=0,downloaded=0)
                state['downloaded'] = state.get('downloaded',0)+1
        state.update(cache=self.media.summary(),memory=memory_snapshot(),updated=now)
        self.catalog.set_meta('prefetch', state)

    def loop(self, stop):
        while not stop.is_set():
            try:
                with self.lock: self.step()
            except BackgroundPaused:
                pass
            except Exception as error:
                print(json.dumps({'event':'prefetch_error','type':type(error).__name__}),flush=True)
            stop.wait(self.interval)
        if self.media.scan is not None:
            self.media.scan.close(); self.media.scan=None
