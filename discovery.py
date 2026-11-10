"""Continuously discover verified off-chart music and rotate recommendations."""
import datetime
import hashlib
import json
import random
import threading
import time

import requests

from catalog import utc
from library import FOLDERS, artist_names, normalized
from resolver import NAMES

CATEGORIES = ('华语', '欧美', '日语', '粤语', '摇滚', '民谣', '电子', '爵士', '轻音乐', '说唱')
REFRESH_SECONDS = 2 * 3600
MIN_REFRESH_SECONDS = 15 * 60
RECENT_SECONDS = 7 * 86400


def public_seeds():
    """Random public playlists supply discovery seeds, never playback rights."""
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://music.163.com/'}
    seeds = []
    for category in random.sample(CATEGORIES, 3):
        try:
            params = {'cat': category, 'order': random.choice(('hot', 'new')),
                      'limit': 12, 'offset': random.randrange(0, 240, 12)}
            response = requests.get('https://music.163.com/api/playlist/list', params=params,
                                    headers=headers, timeout=(3, 7))
            response.raise_for_status()
            if len(response.content) > 4 * 1024 * 1024:
                continue
            playlists = response.json().get('playlists') or []
            if not playlists:
                continue
            playlist = random.choice(playlists)
            response = requests.get('https://music.163.com/api/playlist/detail',
                                    params={'id': playlist['id']}, headers=headers, timeout=(3, 7))
            response.raise_for_status()
            if len(response.content) > 4 * 1024 * 1024:
                continue
            body = response.json()
            tracks = (body.get('result') or body.get('playlist') or {}).get('tracks') or []
            random.shuffle(tracks)
            for track in tracks[:30]:
                names = '、'.join(a.get('name', '') for a in track.get('artists', track.get('ar')) or [])
                query = (track.get('name', '') + ' ' + names).strip()
                if not track.get('name') or len(query) > 160:
                    continue
                seeds.append({'id': str(track['id']), 'query': query, 'category': category})
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue
    # Round-robin categories, rather than taking all seeds from one playlist.
    groups = {}
    for seed in seeds:
        groups.setdefault(seed['category'], []).append(seed)
    return [group[rank] for rank in range(30) for group in groups.values() if rank < len(group)]


def identity(song):
    # A single recording on another platform should not count as a new discovery.
    value = normalized(song['title']) + '\0' + normalized(song['artist'])
    return hashlib.sha256(value.encode()).hexdigest()


class Discovery:
    def __init__(self, catalog, search):
        self.catalog, self.search = catalog, search
        self.lock = threading.Lock()
        self.select_lock = threading.Lock()
        self.wake = threading.Event()
        with catalog.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS discoveries (id TEXT PRIMARY KEY, added REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS recommendation_history (identity TEXT PRIMARY KEY, shown REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS discovery_seeds (id TEXT PRIMARY KEY, checked REAL NOT NULL);
            ''')

    def loop(self, stop):
        while not stop.is_set():
            state = self.catalog.status().get('discovery', {})
            elapsed = time.time() - state.get('finished_epoch', 0)
            if elapsed >= REFRESH_SECONDS or (self.wake.is_set() and elapsed >= MIN_REFRESH_SECONDS):
                self.refresh(stop)
            stop.wait(30)

    def refresh(self, stop=None):
        if not self.lock.acquire(blocking=False):
            return
        stop = stop if stop is not None else threading.Event()
        state = {'running': True, 'started': utc(), 'seeds': 0, 'new_tracks': 0, 'categories': []}
        self.catalog.set_meta('discovery', state)
        try:
            seeds = public_seeds()
            with self.catalog.connect() as db:
                used = {r['id'] for r in db.execute('SELECT id FROM discovery_seeds WHERE checked>?', (time.time() - 30 * 86400,))}
                chart_ids = {key for r in db.execute('SELECT members FROM boards') for key in json.loads(r['members'])}
                previous = {r['id'] for r in db.execute('SELECT id FROM discoveries')}
            selected = []
            for seed in seeds:
                if seed['id'] not in used:
                    selected.append(seed)
                    used.add(seed['id'])
                if len(selected) >= 6:
                    break
            seeds = selected
            if not seeds:
                raise ValueError('no_new_public_seeds')
            added = set()
            for seed in seeds:
                if stop.is_set():
                    break
                keys = self.search.search(seed['query'])
                now = time.time()
                with self.catalog.connect() as db:
                    # Keep only songs with complete audio verification, not an
                    # unverified local match that an online search fell back to.
                    for key in keys:
                        if key in chart_ids:
                            continue
                        row = db.execute("SELECT media FROM songs WHERE id=? AND status='accepted'", (key,)).fetchone()
                        if row and json.loads(row['media']).get('duration_verified'):
                            db.execute('INSERT OR REPLACE INTO discoveries VALUES (?,?)', (key, now))
                            added.add(key)
                    db.execute('INSERT OR REPLACE INTO discovery_seeds VALUES (?,?)', (seed['id'], now))
                state['seeds'] += 1
                if seed['category'] not in state['categories']:
                    state['categories'].append(seed['category'])
            with self.catalog.connect() as db:
                db.execute('DELETE FROM discoveries WHERE id IN (SELECT id FROM discoveries ORDER BY added DESC LIMIT -1 OFFSET 2000)')
                db.execute('DELETE FROM discovery_seeds WHERE checked<?', (time.time() - 30 * 86400,))
                state['pool_tracks'] = db.execute('SELECT count(*) FROM discoveries').fetchone()[0]
            state['new_tracks'] = len(added - previous)
            self.wake.clear()
        except Exception as error:
            state['error'] = type(error).__name__
        finally:
            state.update(running=False, finished=utc(), finished_epoch=time.time())
            self.catalog.set_meta('discovery', state)
            print(json.dumps({'event': 'discovery_refresh', **state}, ensure_ascii=False), flush=True)
            self.lock.release()

    def select(self, library, size, params):
        # Homepage cards may request recommendations in parallel.
        with self.select_lock:
            return self._select(library, size, params)

    def _select(self, library, size, params):
        source = FOLDERS.get(params.get('musicFolderId'))
        songs = [s for s in library.songs.values() if (not source or s['genre'] == NAMES[source])
                 and (not params.get('genre') or s['genre'] == params['genre'])]
        now = time.time()
        with self.catalog.connect() as db:
            pool = {r['id'] for r in db.execute('SELECT id FROM discoveries')}
            shown = {r['identity']: r['shown'] for r in db.execute('SELECT * FROM recommendation_history')}
            played = {r['id']: r['last_played'] for r in db.execute('SELECT id,last_played FROM annotations WHERE last_played IS NOT NULL')}
        # Aggregate source copies so playing one version suppresses its twins.
        for song in songs:
            if song['id'] in played:
                when = datetime.datetime.fromisoformat(played[song['id']].replace('Z', '+00:00')).timestamp()
                key = identity(song)
                shown[key] = max(shown.get(key, 0), when)
        random.shuffle(songs)
        songs.sort(key=lambda s: (shown.get(identity(s), 0), s['playCount'] > 0))
        picked, identities, artists = [], set(), {}

        def take(candidates, target, artist_limit=2):
            for song in candidates:
                if len(picked) >= target:
                    break
                key, names = identity(song), artist_names(song['artist'])
                if key in identities or (artist_limit and any(artists.get(n, 0) >= artist_limit for n in names)):
                    continue
                picked.append(song)
                identities.add(key)
                for name in names:
                    artists[name] = artists.get(name, 0) + 1

        fresh = [s for s in songs if shown.get(identity(s), 0) < now - RECENT_SECONDS]
        exploration = [s for s in fresh if s['id'] in pool]
        take(exploration, min(size, (size * 4 + 4) // 5))
        take(fresh, size)
        # If the catalog is small, relax the artist limit before repeating songs.
        take(fresh, size, None)
        take(songs, size)
        take(songs, size, None)
        random.shuffle(picked)
        if size and len(exploration) < max(50, size):
            self.wake.set()
        if picked:
            with self.catalog.connect() as db:
                db.executemany('INSERT OR REPLACE INTO recommendation_history VALUES (?,?)',
                               ((identity(s), now) for s in picked))
        print(json.dumps({'event': 'recommendation', 'returned': len(picked),
                          'discovered': sum(s['id'] in pool for s in picked),
                          'recent_repeats': sum(shown.get(identity(s), 0) >= now - RECENT_SECONDS for s in picked)}), flush=True)
        return picked
