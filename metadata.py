"""Bounded public lyric/cover lookup with strict cross-source matching and caching."""
import base64
import html
import json
import os
from pathlib import Path
import re
import threading
import time
import weakref
from urllib.parse import urljoin, urlsplit

import requests

from library import artist_names, normalized
from resolver import seconds
from storage import MAX_IMAGES, require_space, sweep

TIME_TAG = re.compile(r'\[(\d{1,3}):(\d{1,2}(?:\.\d{1,3})?)\]')
ALLOWED_HOSTS = ('music.163.com', 'music.126.net', 'kuwo.cn', 'kugou.com',
                 'qq.com', 'gtimg.cn', 'migu.cn', 'migu.com')


def allowed_url(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme in ('http', 'https') and not parsed.username and any(host == d or host.endswith('.' + d) for d in ALLOWED_HOSTS)


def same_recording(record, candidate):
    duration, other = seconds(record), seconds(candidate)
    return (normalized(record['title']) == normalized(candidate['title'])
            and bool(artist_names(record.get('singer', '')) & artist_names(candidate.get('singer', '')))
            and (not duration or not other or abs(duration - other) <= max(8, duration * .04)))


def structured_lyrics(text, artist, title):
    synced, plain = [], []
    offset = re.search(r'\[offset:([+-]?\d+)\]', text, re.I)
    for line in text.splitlines():
        stamps = TIME_TAG.findall(line)
        value = html.unescape(TIME_TAG.sub('', line)).strip()
        if stamps:
            for minutes, second in stamps:
                synced.append({'start': round((int(minutes) * 60 + float(second)) * 1000), 'value': value})
        elif value and not re.fullmatch(r'\[[A-Za-z]+:.*\]', value):
            plain.append({'value': value})
    if not synced and not plain:
        return []
    if synced:
        synced.sort(key=lambda line: line['start'])
    return [{'displayArtist': artist, 'displayTitle': title, 'lang': 'und',
             'offset': int(offset[1]) if offset else 0, 'synced': bool(synced), 'line': synced or plain}]


class Fetch:
    def __init__(self, budget=8):
        self.deadline = time.monotonic() + budget

    def raw(self, url, params=None, headers=None, limit=1024 * 1024):
        if not allowed_url(url):
            raise ValueError('unexpected_metadata_host')
        remaining = self.deadline - time.monotonic()
        if remaining < .2:
            raise TimeoutError('metadata_deadline')
        timeout = (min(2, remaining / 2), min(3, remaining / 2))
        with requests.get(url, params=params, headers={'User-Agent': 'Mozilla/5.0', **(headers or {})},
                          stream=True, timeout=timeout) as response:
            response.raise_for_status()
            if not allowed_url(response.url):
                raise ValueError('unexpected_metadata_redirect')
            content = bytearray()
            for chunk in response.iter_content(16384):
                content.extend(chunk)
                if len(content) > limit or time.monotonic() > self.deadline:
                    raise ValueError('metadata_limit')
            return bytes(content)

    def text(self, url, params=None, headers=None):
        raw = self.raw(url, params, headers)
        try:
            return raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            return raw.decode('gb18030')

    def json(self, url, params=None, headers=None):
        return json.loads(self.text(url, params, headers))


def netease_metadata(fetch, song_id):
    lyric = fetch.json('https://music.163.com/api/song/lyric',
                       {'id': song_id, 'lv': -1, 'kv': -1, 'tv': -1})
    result = {'lrc': (lyric.get('lrc') or {}).get('lyric') or '', 'cover_url': ''}
    try:
        detail = fetch.json('https://music.163.com/api/song/detail/', {'ids': json.dumps([int(song_id)])})
        song = (detail.get('songs') or [{}])[0]
        result['cover_url'] = (song.get('album') or song.get('al') or {}).get('picUrl') or ''
    except (requests.RequestException, ValueError, TimeoutError, KeyError):
        pass
    return result


def matching_netease(fetch, record):
    response = fetch.json('https://music.163.com/api/search/get',
                          {'s': record['title'] + ' ' + record.get('singer', ''), 'type': 1, 'limit': 8})
    for song in (response.get('result') or {}).get('songs') or []:
        artists = '、'.join(a.get('name', '') for a in song.get('artists') or [])
        duration = int(song.get('duration') or 0) / 1000
        if (normalized(song.get('name', '')) == normalized(record['title'])
                and artist_names(artists) & artist_names(record.get('singer', ''))
                and (not duration or not seconds(record) or abs(duration - seconds(record)) <= max(8, seconds(record) * .04))):
            return song['id']
    return None


def source_metadata(fetch, record):
    source, song_id = record['source'], record['id']
    if source == 'netease':
        return netease_metadata(fetch, song_id)
    if source == 'qq':
        body = fetch.json('https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg',
                          {'songmid': song_id, 'format': 'json', 'platform': 'yqq.json', 'nobase64': 1},
                          {'Referer': 'https://y.qq.com/'})
        lyric = body.get('lyric') or ''
        if lyric and not TIME_TAG.search(lyric):
            try:
                lyric = base64.b64decode(lyric, validate=True).decode('utf-8')
            except (ValueError, UnicodeDecodeError):
                pass
        return {'lrc': html.unescape(lyric), 'cover_url': ''}
    if source == 'kuwo':
        body = fetch.json('https://m.kuwo.cn/newh5/singles/songinfoandlrc',
                          {'musicId': song_id, 'httpsStatus': 1}, {'Referer': 'https://www.kuwo.cn/'})
        data = body.get('data') or {}
        lines = []
        for line in data.get('lrclist') or []:
            second = float(line.get('time') or 0)
            lines.append(f'[{int(second // 60):02d}:{second % 60:05.2f}]' + str(line.get('lineLyric') or ''))
        cover = (data.get('songinfo') or {}).get('pic') or ''
        cover = re.sub(r'^https?://img(\d+)\.kwcdn\.kuwo\.cn/', r'https://img\1.kuwo.cn/', cover)
        return {'lrc': '\n'.join(lines), 'cover_url': cover}
    if source == 'kugou':
        result = {'lrc': '', 'cover_url': ''}
        try:
            info = fetch.json('http://m.kugou.com/app/i/getSongInfo.php', {'cmd': 'playInfo', 'hash': song_id})
            result['cover_url'] = (info.get('album_img') or info.get('imgUrl') or '').replace('{size}', '300')
        except (requests.RequestException, ValueError, TimeoutError):
            pass
        body = fetch.json('http://krcs.kugou.com/search',
                          {'ver': 1, 'client': 'mobi', 'hash': song_id, 'duration': seconds(record) * 1000})
        candidates = body.get('candidates') or []
        if candidates:
            candidate = candidates[0]
            response = fetch.json('http://lyrics.kugou.com/download',
                                  {'ver': 1, 'client': 'pc', 'id': candidate['id'],
                                   'accesskey': candidate['accesskey'], 'fmt': 'lrc', 'charset': 'utf8'})
            result['lrc'] = base64.b64decode(response.get('content') or '', validate=True).decode('utf-8-sig')
        return result
    if source == 'migu':
        import migu
        body = fetch.json(migu.SEARCH_API, migu.search_params(record['title'] + ' ' + record.get('singer', ''), 15), migu.SEARCH_HEADERS)
        for item in (body.get('songResultData') or {}).get('result') or []:
            if str(item.get('contentId')) != str(song_id):
                continue
            cover = next((i.get('img') for i in item.get('imgItems') or [] if i.get('img')), '')
            url = item.get('lyricUrl') or item.get('trcUrl') or ''
            lyric = fetch.text(urljoin('https://music.migu.cn/', url)) if url else ''
            return {'lrc': lyric, 'cover_url': cover}
    return {'lrc': '', 'cover_url': ''}


class MetadataCache:
    def __init__(self, catalog):
        self.catalog = catalog
        self.folder = catalog.folder / 'metadata'
        self.folder.mkdir(exist_ok=True)
        self.guard = threading.Lock()
        self.locks = weakref.WeakValueDictionary()
        self.slots = threading.BoundedSemaphore(3)
        with catalog.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata (id TEXT PRIMARY KEY,data TEXT NOT NULL,checked REAL NOT NULL)')

    def cached(self, key):
        with self.catalog.connect() as db:
            row = db.execute('SELECT * FROM metadata WHERE id=?', (key,)).fetchone()
        if row:
            result = json.loads(row['data'])
            ttl = 86400 if result.get('lrc') or result.get('cover_url') else 600
            if time.time() - row['checked'] < ttl:
                return result
        return None

    def get(self, key):
        record = self.catalog.song(key)
        cached = self.cached(key)
        if cached is not None:
            return cached
        with self.guard:
            lock = self.locks.setdefault(key, threading.Lock())
        if not lock.acquire(timeout=8):
            return {}
        try:
            cached = self.cached(key)
            if cached is not None:
                return cached
            if not self.slots.acquire(blocking=False):
                return {}
            try:
                fetch = Fetch()
                result = {'lrc': '', 'cover_url': '', 'matched_source': record['source'], 'matched_id': str(record['id'])}
                try:
                    result.update(source_metadata(fetch, record))
                except (requests.RequestException, ValueError, TimeoutError, KeyError, TypeError):
                    pass
                # An already indexed copy from another source has an exact ID.
                # Prefer it to a fuzzy global search, preserving title/version,
                # artist identity and duration before borrowing its LRC/cover.
                if not result['lrc'] or not result['cover_url']:
                    with self.catalog.connect() as db:
                        alternatives = []
                        for row in db.execute("SELECT data FROM songs WHERE status='accepted' AND id!=?", (key,)):
                            candidate = json.loads(row['data'])
                            if candidate['source'] != record['source'] and same_recording(record, candidate):
                                alternatives.append(candidate)
                    preference = {'netease': 0, 'kugou': 1, 'qq': 2, 'migu': 3, 'kuwo': 4}
                    alternatives.sort(key=lambda r: preference[r['source']])
                    for alternate in alternatives[:2]:
                        try:
                            supplement = source_metadata(fetch, alternate)
                            for field in ('lrc', 'cover_url'):
                                if not result.get(field):
                                    result[field] = supplement.get(field) or ''
                            result.update(matched_source=alternate['source'], matched_id=str(alternate['id']))
                        except (requests.RequestException, ValueError, TimeoutError, KeyError, TypeError):
                            continue
                        if result['lrc'] and result['cover_url']:
                            break
                if record['source'] != 'netease' and (not result['lrc'] or not result['cover_url']):
                    try:
                        match = matching_netease(fetch, record)
                        if match:
                            supplement = netease_metadata(fetch, match)
                            for field in ('lrc', 'cover_url'):
                                if not result.get(field):
                                    result[field] = supplement.get(field) or ''
                            result.update(matched_source='netease', matched_id=str(match))
                    except (requests.RequestException, ValueError, TimeoutError, KeyError, TypeError):
                        pass
                text = str(result.get('lrc') or '').strip().lstrip('\ufeff')[:200000]
                if text.startswith(('<', '{')):
                    text = ''
                result['lrc'] = text
                if not allowed_url(result.get('cover_url') or ''):
                    result['cover_url'] = ''
                with self.catalog.connect() as db:
                    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?,?)', (key, json.dumps(result, ensure_ascii=False), time.time()))
                return result
            finally:
                self.slots.release()
        finally:
            lock.release()

    def cover(self, key):
        # Cover requests can arrive in bulk while browsing. Only fetch images for
        # already resolved metadata; lyric lookup populates this cache on demand.
        cached = self.cached(key) or {}
        if not cached.get('cover_url'):
            return None
        path = self.folder / (key + '.image')
        try:
            raw = path.read_bytes() if path.exists() and time.time() - path.stat().st_mtime < 86400 else None
        except FileNotFoundError:
            raw = None
        if raw is None:
            if not self.slots.acquire(blocking=False):
                return None
            try:
                require_space(self.catalog.folder, 2 * 1024 * 1024)
                raw = Fetch(3).raw(cached['cover_url'], limit=2 * 1024 * 1024)
                if not raw.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n')):
                    return None
                temporary = self.folder / (key + '.' + str(threading.get_ident()) + '.part')
                try:
                    temporary.write_bytes(raw)
                    os.replace(temporary, path)
                    with self.guard:
                        sweep(self.folder, MAX_IMAGES, 86400)
                finally:
                    temporary.unlink(missing_ok=True)
            except (requests.RequestException, ValueError, TimeoutError):
                return None
            finally:
                self.slots.release()
        return raw, 'image/png' if raw.startswith(b'\x89PNG') else 'image/jpeg'
