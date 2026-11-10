"""On-demand bounded audio cache with real duration checks and Range delivery."""
import os
from pathlib import Path
import threading
import time
import weakref
from contextlib import contextmanager

import requests

from resolver import inspect_audio, resolve, seconds
from storage import require_space, sweep

MAX_FILE = 40 * 1024 * 1024
MAX_CACHE = 512 * 1024 * 1024


class MediaCache:
    def __init__(self, catalog):
        self.catalog = catalog
        self.folder = catalog.folder / 'audio'
        self.folder.mkdir(exist_ok=True)
        self.guard = threading.Lock()
        self.locks = weakref.WeakValueDictionary()
        self.pins = {}
        self.slots = threading.BoundedSemaphore(3)

    def cleanup(self):
        def protected(path):
            key = path.name.split('.')[0]
            lock = self.locks.get(key)
            return self.pins.get(key, 0) > 0 or bool(lock and lock.locked())
        return sweep(self.folder, MAX_CACHE, 86400, protected)

    @contextmanager
    def open_audio(self, song_id):
        with self.guard:
            self.pins[song_id] = self.pins.get(song_id, 0) + 1
        try:
            path = self.prepare(song_id)
            with path.open('rb') as stream:
                yield path, stream
        finally:
            with self.guard:
                self.pins[song_id] -= 1
                if self.pins[song_id] == 0:
                    del self.pins[song_id]

    def prepare(self, song_id):
        record = self.catalog.song(song_id)
        with self.guard:
            lock = self.locks.setdefault(song_id, threading.Lock())
        with lock:
            ready = next((p for p in self.folder.glob(song_id + '.*') if p.suffix != '.part'), None)
            if ready and time.time() - ready.stat().st_mtime < 86400:
                return ready
            if not self.slots.acquire(timeout=25):
                raise ValueError('音频准备繁忙，请稍后重试')
            partial = self.folder / (song_id + '.part')
            try:
                with self.guard:
                    self.cleanup()
                require_space(self.catalog.folder, MAX_FILE)
                url, headers, suffix = resolve(record)
                with requests.get(url, headers=headers, stream=True, timeout=(8, 25)) as response:
                    response.raise_for_status()
                    if int(response.headers.get('Content-Length') or 0) > MAX_FILE:
                        raise ValueError('音频文件超过实验版大小限制')
                    received = 0
                    with partial.open('wb') as output:
                        for chunk in response.iter_content(65536):
                            received += len(chunk)
                            if received > MAX_FILE:
                                raise ValueError('音频文件超过实验版大小限制')
                            output.write(chunk)
                info = inspect_audio(partial)
                duration = float(info.get('format', {}).get('duration') or 0)
                if not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])) or duration <= 0:
                    raise ValueError('未取得有效音频')
                if seconds(record) > duration + max(8, seconds(record) * .03):
                    self.catalog.exclude(song_id, 'short_preview_at_playback')
                    raise ValueError('仅取得试听片段，已从音乐库排除')
                target = self.folder / (song_id + '.' + suffix)
                os.replace(partial, target)
                with self.guard:
                    self.cleanup()
                return target
            finally:
                partial.unlink(missing_ok=True)
                self.slots.release()
