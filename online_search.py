"""Bounded, cached public-source search; only verified complete audio is admitted."""
from concurrent.futures import ThreadPoolExecutor, wait
import datetime
import hashlib
import json
import threading
import time
import unicodedata

import requests

from catalog import identifier, utc
from library import FOLDERS, matches, normalized
from resolver import NAMES, blocked, probe, seconds
from storage import require_space

SEARCH_SECONDS = 25
CACHE_SECONDS = 300


def source_search(source, query):
    if source != 'netease':
        import importlib
        return importlib.import_module(source).search(query)
    # Keep membership metadata, which the old vendor search wrapper discards.
    response = requests.get('https://music.163.com/api/search/get',
        params={'s': query, 'type': 1, 'offset': 0, 'limit': 8},
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://music.163.com/'}, timeout=(3, 7))
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError('search_response_too_large')
    body = response.json()
    if body.get('code') != 200:
        raise ValueError('search_unavailable')
    songs = []
    for item in (body.get('result') or {}).get('songs') or []:
        privilege = item.get('privilege') or {}
        paid = item.get('fee') in (1, 4) or bool(privilege.get('chargeInfoList') and privilege.get('fl') == 0)
        songs.append({'source': source, 'id': str(item['id']), 'title': item.get('name', ''),
            'singer': '、'.join(a['name'] for a in item.get('artists', item.get('ar')) or []),
            'album': (item.get('album') or item.get('al') or {}).get('name', ''),
            'duration_seconds': round((item.get('duration') or item.get('dt') or 0) / 1000),
            'playable': not paid, 'downloadable': not paid,
            'availability': '需会员' if paid else ''})
    return songs


class OnlineSearch:
    def __init__(self, catalog):
        self.catalog = catalog
        self.workers = ThreadPoolExecutor(max_workers=10, thread_name_prefix='search-source')
        self.queries = ThreadPoolExecutor(max_workers=2, thread_name_prefix='search-query')
        self.guard = threading.RLock()
        self.flights = {}
        with catalog.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS searches (key TEXT PRIMARY KEY, ids TEXT NOT NULL, expires REAL NOT NULL, stats TEXT NOT NULL)')

    def close(self):
        self.queries.shutdown(wait=False, cancel_futures=True)
        self.workers.shutdown(wait=False, cancel_futures=True)

    def cached(self, key):
        with self.catalog.connect() as db:
            row = db.execute('SELECT ids FROM searches WHERE key=? AND expires>?', (key, time.time())).fetchone()
        return json.loads(row['ids']) if row else None

    def search(self, query, folder=None):
        source = FOLDERS.get(str(folder))
        canonical = ' '.join(unicodedata.normalize('NFKC', query).casefold().split())
        key = hashlib.sha256((str(source) + '\0' + canonical).encode()).hexdigest()
        cached = self.cached(key)
        if cached is not None:
            return cached
        with self.guard:
            future = self.flights.get(key)
            if future is None:
                # Do not queue unbounded searches if clients search as they type.
                if len(self.flights) >= 2:
                    return self.local(query, source)
                future = self.queries.submit(self.run, key, query, source)
                self.flights[key] = future
                future.add_done_callback(lambda done: self.finished(key, done))
        try:
            return future.result(timeout=SEARCH_SECONDS + 1)
        except TimeoutError:
            return self.local(query, source)

    def finished(self, key, future):
        with self.guard:
            if self.flights.get(key) is future:
                del self.flights[key]

    def local(self, query, source):
        keys = []
        for row in self.catalog.iter_visible():
            record = row['record']
            album = record.get('album') or NAMES[record['source']] + ' · 榜单精选'
            artist = record.get('singer') or '未知歌手'
            if (not source or record['source'] == source) and matches(record['title'] + ' ' + artist + ' ' + album, query):
                keys.append(row['key'])
        return keys

    def check(self, song):
        key = identifier(song['source'], song['id'])
        with self.catalog.connect() as db:
            row = db.execute('SELECT status,media,checked FROM songs WHERE id=?', (key,)).fetchone()
        if row and row['status'] == 'accepted' and row['checked']:
            media = json.loads(row['media'])
            checked = datetime.datetime.fromisoformat(row['checked'].replace('Z', '+00:00')).timestamp()
            if media.get('duration_verified') and time.time() - checked < 12 * 3600:
                return song, media
        return song, probe(song, verify_duration=True)

    def run(self, key, query, source):
        deadline = time.monotonic() + SEARCH_SECONDS
        sources = [source] if source else list(NAMES)
        futures = {self.workers.submit(source_search, name, query): name for name in sources}
        done, pending = wait(futures, timeout=min(10, max(0, deadline - time.monotonic())))
        for future in pending:
            future.cancel()
        results, stats = {}, {'sources': {}, 'blocked': 0, 'unverified': 0, 'verified': 0}
        for future in done:
            name = futures[future]
            try:
                results[name] = future.result()[:8]
                stats['sources'][name] = 'ok'
            except Exception as error:
                stats['sources'][name] = type(error).__name__
        for future in pending:
            stats['sources'][futures[future]] = 'timeout'
        candidates, seen = [], set()
        # Interleave platforms so a slow/failing source cannot dominate the page.
        for rank in range(8):
            for name in sources:
                listing = results.get(name, [])
                if rank >= len(listing):
                    continue
                song = listing[rank]
                song_id = identifier(name, song.get('id'))
                if song_id in seen or song.get('source') != name or not song.get('title'):
                    continue
                seen.add(song_id)
                if blocked(song):
                    stats['blocked'] += 1
                    self.catalog.exclude(song_id, 'membership_or_trial_in_search')
                elif seconds(song) <= 0:
                    stats['unverified'] += 1
                else:
                    candidates.append(song)
        checks = [self.workers.submit(self.check, song) for song in candidates]
        done, pending = wait(checks, timeout=max(0, deadline - time.monotonic()))
        for future in pending:
            future.cancel()
        admitted = []
        for song, future in zip(candidates, checks):
            if future not in done:
                stats['unverified'] += 1
                continue
            try:
                song, media = future.result()
                song_id = identifier(song['source'], song['id'])
                require_space(self.catalog.folder)
                with self.catalog.connect() as db:
                    db.execute("INSERT INTO songs(id,data,status,media,reason,checked,touched) VALUES (?,?,'accepted',?,'',?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,status='accepted',media=excluded.media,reason='',checked=excluded.checked,touched=excluded.touched",
                               (song_id, json.dumps(song, ensure_ascii=False), json.dumps(media), utc(), time.time()))
                admitted.append(song_id)
                stats['verified'] += 1
            except Exception:
                stats['unverified'] += 1
                self.catalog.exclude(identifier(song['source'], song['id']), 'unverified_in_search')
        ids = list(dict.fromkeys(admitted + self.local(query, source)))
        titles = {r['key']: r['record']['title'] for r in self.catalog.iter_visible(ids)}
        ids = [key for key in ids if key in titles]
        # Exact titles first; stable platform rank retained otherwise, including pagination.
        ids.sort(key=lambda k: normalized(titles[k]) != normalized(query))
        ttl = CACHE_SECONDS if results else 30
        with self.catalog.connect() as db:
            db.execute('DELETE FROM searches WHERE expires<?', (time.time(),))
            db.execute('INSERT OR REPLACE INTO searches VALUES (?,?,?,?)',
                       (key, json.dumps(ids), time.time() + ttl, json.dumps(stats)))
        print(json.dumps({'event': 'online_search', **stats, 'returned': len(ids)}, ensure_ascii=False), flush=True)
        return ids
