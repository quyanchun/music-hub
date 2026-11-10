"""QQ Music search with playback URLs supplied by the LX Music source ecosystem.

Interface references (read, never executed):
  - lyswhut/lx-music-desktop  src/renderer/utils/musicSdk/tx/musicSearch.js
  - pdone/lx-music-source     huibq/latest.js  (lxmusicapi.onrender.com)
  - pdone/lx-music-source     jiexiang/latest.js (tang.api.s01s.cn fallback)

QQ Music's own vkey endpoint returns an empty purl for anonymous callers
(verified 2026-11-07), so audio URLs come from LX custom-source relays.
These are third-party services and may disappear; MUSIC_DL_QQ_URL_API and
MUSIC_DL_QQ_URL_BACKUP let a deployment point at a self-hosted LX API server
instead. Search metadata always comes from the platform itself.

Only URLs on QQ Music's own streaming hosts are accepted: some relays return a
fixed fallback file when resolution fails, which must never be saved as a song.
"""
import datetime
import html
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://y.qq.com/'}
STREAM_HOSTS = ('qqmusic.qq.com', 'qmusic.qq.com')
AUDIO_EXTENSIONS = ('mp3', 'm4a', 'ogg', 'wav', 'flac', 'ape')
RELAY_TIMEOUT = (8, 14)

# {songmid} is replaced with the QQ songmid, e.g. 003os5gx3zLAc5.
PRIMARY_API = os.environ.get('MUSIC_DL_QQ_URL_API',
                             'https://lxmusicapi.onrender.com/url/tx/{songmid}/128k')
BACKUP_API = os.environ.get('MUSIC_DL_QQ_URL_BACKUP',
                            'https://tang.api.s01s.cn/music_open_api.php')


def clean_text(value):
    return html.unescape(str(value or '')).replace('\xa0', ' ').strip()


def search(keyword):
    response = requests.get('https://c.y.qq.com/soso/fcgi-bin/client_search_cp',
                            params={'w': keyword, 'format': 'json', 'p': 1, 'n': 8,
                                    'aggr': 1, 'cr': 1, 'new_json': 1, 'platform': 'yqq'},
                            headers=HEADERS, timeout=8)
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError('搜索响应异常')
    data = response.json()
    songs = []
    for item in ((data.get('data') or {}).get('song') or {}).get('list', []):
        songmid = item.get('mid') or ''
        if not re.fullmatch(r'[A-Za-z0-9]{6,32}', songmid):
            continue
        media = item.get('file') or {}
        pay = item.get('pay') or {}
        paid = int(pay.get('pay_play') or 0) != 0
        available = not paid and int(media.get('size_128') or 0) > 0
        seconds = int(item.get('interval') or 0)
        singers = [clean_text(s.get('name')) for s in item.get('singer', []) if s.get('name')]
        songs.append({'source': 'qq', 'id': songmid,
                      'title': clean_text(item.get('name')),
                      'singer': '、'.join(singers),
                      'album': clean_text((item.get('album') or {}).get('name')),
                      'duration': str(datetime.timedelta(seconds=seconds)),
                      'duration_seconds': seconds, 'hash': '',
                      'playable': available, 'downloadable': available,
                      'availability': '需会员' if paid else '' if available else '暂不可用'})
    return songs


def _valid_audio(url):
    """Reject relay fallbacks and non-audio links."""
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        return False
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if not any(host == allowed or host.endswith('.' + allowed) for allowed in STREAM_HOSTS):
        return False
    extension = parsed.path.rpartition('.')[2].lower()
    return extension in AUDIO_EXTENSIONS


def _urls_in(text):
    # Accept both plain URLs and JSON-escaped "https:\/\/..." strings.
    candidates = re.findall(r'https?:[^\s"\'<>,}\]]+', text)
    return [url for url in (candidate.replace('\\/', '/') for candidate in candidates)
            if _valid_audio(url)]


def resolve_audio(songmid):
    """Return (url, extension) from the first relay that resolves this song."""
    reason = '解析服务未返回音频'
    try:
        response = requests.get(PRIMARY_API.format(songmid=songmid),
                                headers={**HEADERS, 'X-Request-Key': 'share-v3'},
                                timeout=RELAY_TIMEOUT)
        response.raise_for_status()
        body = response.json()
        url = body.get('url')
        # A failed resolution on this relay still carries code 0 and a fixed
        # fallback URL; require an explicit success message plus a QQ host.
        if (body.get('code') == 0 and str(body.get('msg', '')).lower() == 'success'
                and _valid_audio(url)):
            return url, urlsplit(url).path.rpartition('.')[2].lower()
        reason = '主通道：' + str(body.get('msg') or '未返回音频')
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        reason = f'主通道异常：{error}'

    try:
        response = requests.get(BACKUP_API, params={'mid': songmid},
                                headers=HEADERS, timeout=RELAY_TIMEOUT)
        response.raise_for_status()
        urls = _urls_in(response.text)
        # Prefer the standard MP3, then AAC; skip lossless to bound file size.
        url = next((u for u in urls if urlsplit(u).path.lower().endswith('.mp3')), None) or \
              next((u for u in urls if urlsplit(u).path.lower().endswith('.m4a')), None)
        if url:
            return url, urlsplit(url).path.rpartition('.')[2].lower()
        reason = '备用通道未返回音频'
    except requests.RequestException as error:
        reason = f'备用通道异常：{error}'
    raise ValueError(f'QQ 音乐解析服务暂时不可用（{reason}），请稍后重试或试试其他来源')


def download(record, directory, progress_path=None):
    if record.get('playable') is False:
        raise ValueError('这首歌暂时无法试听或下载')
    url, extension = resolve_audio(record['id'])
    name = re.sub(r'[\\/:*?"<>|]', '', f"{record['singer']} - {record['title']}").strip()[:70]
    path = Path(directory) / ((name or 'audio') + '.' + extension)
    from audio_transfer import download as transfer
    transfer(url, path, HEADERS, progress_path)
    return None  # bitrate is measured from the real file by ffprobe in worker.py
