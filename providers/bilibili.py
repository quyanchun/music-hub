"""Public Bilibili AU audio and AM playlists; no BV extraction or member cookies."""
import html
import json
import re
import threading
import time

import requests

BASE = 'https://www.bilibili.com/audio/music-service-c/web/'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
API_LOCK = threading.Lock()
NEXT_REQUEST = 0


def api(path, params):
    global NEXT_REQUEST
    with API_LOCK:
        time.sleep(max(0, NEXT_REQUEST - time.monotonic()))
        NEXT_REQUEST = time.monotonic() + 3
    return fetch(path, params)


def fetch(path, params):
    with requests.get(BASE + path, params=params, headers=HEADERS,
                      timeout=(3, 7), stream=True) as response:
        response.raise_for_status()
        raw = bytearray()
        for chunk in response.iter_content(32768):
            raw.extend(chunk)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('bilibili_response_too_large')
    body = json.loads(raw)
    if body.get('code') != 0 or not isinstance(body.get('data'), dict):
        raise ValueError('bilibili_audio_unavailable')
    return body['data']


def track(row):
    return {'source': 'bilibili', 'id': str(row['id']),
            'title': html.unescape(row.get('title') or '').strip(),
            'singer': html.unescape(row.get('author') or row.get('uname') or '').strip(),
            'album': 'B站音频区', 'duration_seconds': int(row.get('duration') or 0),
            'playable': not bool(row.get('limit')), 'downloadable': not bool(row.get('limit')),
            'cover_url': row.get('cover') or '', 'lyric_url': row.get('lyric') or ''}


def list_playlists():
    page = api('menu/hit', {'ps': 12, 'pn': 1})
    return [{'source': 'bilibili', 'id': str(row['menuId']),
             'name': html.unescape(row.get('title') or '').strip(), 'count': 0}
            for row in (page.get('data') or [])[:12]]


def playlist_tracks(key, offset, limit):
    page = api('song/of-menu', {'sid': key, 'ps': limit, 'pn': offset // limit + 1})
    total = int(page.get('totalSize') or 0)
    return {'songs': [track(row) for row in page.get('data') or []], 'total': total,
            'limited': False, 'has_more': offset + limit < total}


def search(query):
    # AU lookup is supported. General keywords search the already collected AU
    # catalog locally; the separate video search endpoint currently returns 412.
    match = re.fullmatch(r'(?:https?://www\.bilibili\.com/audio/)?au(\d+)/?', query.strip(), re.I)
    return [track(api('song/info', {'sid': match[1]}))] if match else []


def resolve_audio(key):
    info = api('song/info', {'sid': key})
    if str(info.get('id')) != str(key) or info.get('limit') or not info.get('duration'):
        raise ValueError('bilibili_audio_restricted')
    audio = api('url', {'sid': key})
    if str(audio.get('sid')) != str(key) or not audio.get('cdns'):
        raise ValueError('bilibili_audio_unavailable')
    # The API's size field is unreliable. The shared downloader checks the actual
    # CDN response length and audio duration before registering the cache file.
    return audio['cdns'][0]
