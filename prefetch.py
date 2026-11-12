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
        self.extra_limit = int(os.environ.get('MUSICHUB_EXTRA_CACHE_DAILY', '100'))
        self.lock = threading.Lock()
        self.next_reconcile = 0

    def fill_daily(self, day):
        cutoff = (datetime.date.fromisoformat(day) - datetime.timedelta(days=7)).isoformat()
        with self.catalog.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('SELECT count(*) FROM daily_tracks d JOIN audio_files f ON f.id=d.file_id WHERE day=? AND f.verified=1', (day,)).fetchone()[0]
            if count < self.daily_size:
                rows = db.execute('''SELECT f.id FROM audio_files f WHERE verified=1
                    AND NOT EXISTS(SELECT 1 FROM daily_tracks d WHERE d.file_id=f.id AND d.day>=?)
                    ORDER BY random() LIMIT ?''', (cutoff, self.daily_size-count)).fetchall()
                db.executemany('INSERT OR IGNORE INTO daily_tracks VALUES (?,?)', ((day, r['id']) for r in rows))
                count += len(rows)
            if count == self.daily_size:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('daily_active',?)", (json.dumps(day),))
            db.execute('DELETE FROM daily_tracks WHERE day<?', ((datetime.date.fromisoformat(day)-datetime.timedelta(days=30)).isoformat(),))
            db.execute('DELETE FROM cache_attempts WHERE retry_at<?', (time.time()-7*86400,))
        return count

    def daily(self, size, params):
        day = cache_day()
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
            status = getattr(getattr(error,'response',None),'status_code',None)
            if status in (403,429) or '频繁' in str(error):
                source = db.execute("SELECT json_extract(data,'$.source') FROM songs WHERE id=?",(key,)).fetchone()
                previous = db.execute("SELECT value FROM meta WHERE key='source_backoff'").fetchone()
                cooldown = json.loads(previous['value']) if previous else {}
                if source:
                    cooldown[source[0]] = time.time()+1800
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('source_backoff',?)",(json.dumps(cooldown),))
            db.execute('INSERT OR REPLACE INTO cache_attempts VALUES (?,?,?)',
                       (key, time.time()+min(6*3600, 1800*2**min(failures-1,4)), failures))

    def download(self, key):
        original = self.catalog.song(key)
        try:
            path = self.media.prepare(key, background=True)
            hit = self.media.cached(path.stem, verified=True)
            if hit: return hit[0]['id']
            # Old files are verified one at a time by reconciliation.
            raise BackgroundPaused('等待已有文件完整校验')
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
                alternatives = self.search.search((original['title']+' '+original.get('singer','')).strip())[:64]
            for alternate in alternatives:
                if alternate == key: continue
                try:
                    record = self.catalog.song(alternate)
                    if recording_key(record)!=recording_key(original) or abs(seconds(record)-seconds(original))>3: continue
                    with self.catalog.connect() as db:
                        retry = db.execute('SELECT 1 FROM cache_attempts WHERE song_id=? AND retry_at>?', (alternate,time.time())).fetchone()
                        cooldown = db.execute("SELECT coalesce(json_extract(value,?),0) FROM meta WHERE key='source_backoff'",('$.'+record['source'],)).fetchone()
                    if retry or (cooldown and cooldown[0]>time.time()): continue
                    path = self.media.prepare(alternate, background=True)
                    hit = self.media.cached(path.stem, verified=True)
                    if hit:
                        self.media.bind(key, hit[0]['id'])
                        return hit[0]['id']
                except BackgroundPaused: raise
                except Exception as error: self.retry(alternate,error)
        return None

    def step(self):
        if self.media.players or not background_allowed(): raise BackgroundPaused('前台优先或内存压力')
        day, now = cache_day(), time.time()
        if now >= self.next_reconcile:
            if not self.media.reconcile_one():
                self.next_reconcile = now+1800
        count = self.fill_daily(day)
        with self.catalog.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='prefetch'").fetchone()
            state = json.loads(row['value']) if row else {}
        if state.get('day')!=day: state = {'day':day,'extra':0}
        hour = int(now//3600)
        if state.get('hour')!=hour: state.update(hour=hour,attempts=0)
        local_hour = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).hour
        extra = count >= self.daily_size
        if state['attempts'] >= 80 or (extra and (not 3 <= local_hour < 6 or state['extra']>=self.extra_limit)):
            return
        with self.catalog.connect() as db:
            # A disk-backed random sort, not an in-memory all-song queue.
            row = db.execute('''SELECT s.id FROM songs s
                LEFT JOIN audio_aliases a ON a.song_id=s.id
                LEFT JOIN cache_attempts c ON c.song_id=s.id
                WHERE s.status='accepted' AND a.song_id IS NULL AND coalesce(c.retry_at,0)<=?
                AND coalesce(json_extract((SELECT value FROM meta WHERE key='source_backoff'),'$.'||json_extract(s.data,'$.source')),0)<=?
                AND (?=0 OR s.id IN (SELECT m.value FROM boards b,json_each(b.members) m))
                ORDER BY random() LIMIT 1''', (now, now, int(extra))).fetchone()
        if row is None: return
        state['attempts'] += 1
        self.catalog.set_meta('prefetch',state)
        file_id = self.download(row['id'])
        if file_id:
            if extra: state['extra'] += 1
            else:
                with self.catalog.connect() as db:
                    db.execute('INSERT OR IGNORE INTO daily_tracks VALUES (?,?)', (day,file_id))
                count = self.fill_daily(day)
        state.update(ready=count,cache=self.media.summary(),memory=memory_snapshot(),updated=now)
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
