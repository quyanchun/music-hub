"""Publish bounded public playlists in the existing Subsonic library, off the request path."""
import json
import threading
import time

from catalog import identifier, utc
from memory_budget import background_allowed
from public_sources import list_playlists, playlist_tracks
from resolver import NAMES, blocked, seconds
from storage import require_space

MAX_TRACKS = 300
PAGE_SIZE = 30
REFRESH_SECONDS = 6 * 3600


class PublicPlaylists:
    def __init__(self, catalog):
        self.catalog = catalog
        self.lock = threading.Lock()
        with catalog.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS public_playlist_catalog (id TEXT PRIMARY KEY, source TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL)')

    def admit(self, song):
        source, value = song.get('source'), str(song.get('id') or '')
        if source not in NAMES or not value or value == 'None' or not song.get('title'):
            return None
        key = identifier(source, value)
        if blocked(song):
            self.catalog.exclude(key, 'membership_or_trial_in_playlist')
            return None
        if seconds(song) <= 0:
            return None
        # Collect metadata without per-song audio requests. Preserve confirmed
        # failures across refreshes; untried tracks remain available for playback.
        require_space(self.catalog.folder)
        media = {'suffix': 'm4a' if source == 'bilibili' else 'mp3',
                 'duration': seconds(song), 'size': 0, 'bitrate': 128}
        with self.catalog.connect() as db:
            db.execute("INSERT INTO songs(id,data,status,media,touched) VALUES (?,?,'pending',?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,touched=excluded.touched,status=CASE WHEN songs.status='rejected' THEN 'pending' ELSE songs.status END",
                       (key, json.dumps(song, ensure_ascii=False), json.dumps(media), time.time()))
        return key

    def refresh(self, stop=None):
        stop = stop or threading.Event()
        if not self.lock.acquire(blocking=False):
            return
        state = {'running': True, 'started': utc(), 'sources': {}, 'processed': 0}
        try:
            self.catalog.set_meta('public_playlists', state)
            cards = []
            for source in NAMES:
                while not background_allowed():
                    if stop.wait(10): return
                if stop.is_set():
                    return
                try:
                    listing = list_playlists(source)[:12]
                    keys = []
                    with self.catalog.connect() as db:
                        for card in listing:
                            if card.get('source') != source or not card.get('id') or not card.get('name'):
                                continue
                            key = 'pl-' + source + '-' + identifier(source, card['id'])
                            keys.append(key)
                            cards.append((key, card))
                            db.execute('INSERT OR REPLACE INTO public_playlist_catalog VALUES (?,?,?,?)',
                                       (key, source, json.dumps(card, ensure_ascii=False), time.time()))
                        old = [r['id'] for r in db.execute('SELECT id FROM public_playlist_catalog WHERE source=?', (source,)) if r['id'] not in keys]
                        for key in old:
                            db.execute('DELETE FROM boards WHERE id=?', (key,))
                            db.execute('DELETE FROM public_playlist_catalog WHERE id=?', (key,))
                    state['sources'][source] = {'listed': len(keys), 'ready': 0, 'failed': 0}
                except Exception as error:
                    state['sources'][source] = {'error': type(error).__name__}
            # Round-robin pages let every platform publish a first batch before
            # expanding large playlists. Browsing never waits for upstream I/O.
            active = cards
            if cards:
                for offset in range(0, MAX_TRACKS, PAGE_SIZE):
                    following = []
                    for key, card in active:
                        while not background_allowed():
                            if stop.wait(10): return
                        if stop.is_set():
                            return
                        source = card['source']
                        try:
                            page = playlist_tracks(source, card['id'], offset, PAGE_SIZE)
                            members = list(dict.fromkeys(k for k in map(self.admit, page['songs']) if k))
                            require_space(self.catalog.folder)
                            with self.catalog.connect() as db:
                                old = db.execute('SELECT members FROM boards WHERE id=?', (key,)).fetchone()
                                prefix = json.loads(old['members']) if offset and old else []
                                combined = list(dict.fromkeys(prefix + members))
                                db.execute('INSERT OR REPLACE INTO boards VALUES (?,?,?,?,?,?)', (key, source,
                                    NAMES[source] + ' · 歌单 · ' + card['name'], card.get('count') or page['total'], json.dumps(combined), utc()))
                            if combined and (offset == 0 or not prefix):
                                state['sources'][source]['ready'] += 1
                            if page['has_more']:
                                following.append((key, card))
                        except Exception as error:
                            state['sources'][source]['failed'] += 1
                            if offset == 0:
                                with self.catalog.connect() as db:
                                    db.execute('DELETE FROM boards WHERE id=?', (key,))
                            print(json.dumps({'event': 'playlist_page_error', 'source': source,
                                              'type': type(error).__name__}), flush=True)
                        state['processed'] += 1
                        self.catalog.set_meta('public_playlists', state)
                    active = following
                    if not active:
                        break
            with self.catalog.connect() as db:
                # A failed platform may keep its last snapshot for at most a day.
                stale = [r['id'] for r in db.execute('SELECT id FROM public_playlist_catalog WHERE updated<?', (time.time() - 86400,))]
                for key in stale:
                    db.execute('DELETE FROM boards WHERE id=?', (key,))
                    db.execute('DELETE FROM public_playlist_catalog WHERE id=?', (key,))
        finally:
            state.update(running=False, finished=utc(), finished_epoch=0 if stop.is_set() else time.time())
            try:
                self.catalog.set_meta('public_playlists', state)
                print(json.dumps({'event': 'public_playlists', **state}), flush=True)
            finally:
                self.lock.release()

    def loop(self, stop):
        while not stop.is_set():
            state = self.catalog.status().get('public_playlists', {})
            partial = any(s.get('error') or s.get('failed') for s in state.get('sources', {}).values())
            interval = 900 if partial else REFRESH_SECONDS
            remaining = state.get('finished_epoch', 0) + interval - time.time()
            if set(NAMES) - set(state.get('sources', {})):
                remaining = 0  # Discover a newly enabled source on the next start.
            if remaining > 0 and stop.wait(remaining):
                break
            try:
                self.refresh(stop)
            except Exception as error:
                print(json.dumps({'event': 'playlist_refresh_error', 'type': type(error).__name__}), flush=True)
                stop.wait(900)
