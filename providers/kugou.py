"""Kugou Music search and playback through the platform's own public endpoints.

This adapter was originally disabled because the anonymous playInfo endpoint
returned errcode 1002. Re-verified end to end on 2026-11-08: it now returns
errcode=0 with real sharefs.kugou.com URLs for standard-quality tracks, and an
empty URL with "需要付费" for paid ones. No third-party relay is involved;
availability is decided by the platform at download time.

The song id is the standard-quality file hash itself, so a restored playback
queue (which only keeps id/title/singer/album/duration) can still be downloaded.
"""
import datetime
import html
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

HEADERS = {'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')}
FETCH_HEADERS = {'User-Agent': 'Wget/1.19.5', 'Accept-Encoding': 'identity'}
SEARCH_API = 'http://mobilecdn.kugou.com/api/v3/search/song'
PLAY_API = 'http://m.kugou.com/app/i/getSongInfo.php'
AUDIO_EXTENSIONS = ('mp3', 'm4a', 'flac', 'ape', 'ogg', 'wav')


def clean_text(value):
    return html.unescape(str(value or '')).replace('\xa0', ' ').strip()


def _valid_audio(url):
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        return False
    host = (urlsplit(url).hostname or '').lower()
    if not (host == 'kugou.com' or host.endswith('.kugou.com')):
        return False
    return urlsplit(url).path.rpartition('.')[2].lower() in AUDIO_EXTENSIONS


def search(keyword):
    response = requests.get(SEARCH_API,
                            params={'keyword': keyword, 'page': 1, 'pagesize': 30},
                            headers=HEADERS, timeout=10)
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError('搜索响应异常')
    items = (response.json().get('data') or {}).get('info') or []
    # Keep the platform order inside each group, but surface tracks whose
    # standard quality is free first: paid tracks return an empty playInfo URL.
    ordered = ([i for i in items if int(i.get('pay_type') or 0) == 0]
               + [i for i in items if int(i.get('pay_type') or 0) != 0])
    songs = []
    for item in ordered:
        file_hash = item.get('hash') or ''
        if not re.fullmatch(r'[A-Za-z0-9]{16,40}', file_hash):
            continue
        seconds = int(item.get('duration') or 0)
        paid = int(item.get('pay_type') or 0) != 0
        songs.append({'source': 'kugou', 'id': file_hash,
                      'title': clean_text(item.get('songname')),
                      'singer': clean_text(item.get('singername')),
                      'album': clean_text(item.get('album_name')),
                      'duration': str(datetime.timedelta(seconds=seconds)),
                      'duration_seconds': seconds, 'hash': file_hash,
                      'playable': not paid, 'downloadable': not paid,
                      'availability': '需会员' if paid else ''})
        if len(songs) >= 8:
            break
    return songs


def resolve_audio(file_hash):
    if not re.fullmatch(r'[A-Za-z0-9]{16,40}', str(file_hash or '')):
        raise ValueError('歌曲标识无效')
    response = requests.get(PLAY_API, params={'cmd': 'playInfo', 'hash': file_hash},
                            headers=HEADERS, timeout=10)
    response.raise_for_status()
    audio = response.json()
    if audio.get('errcode') == 1002:
        raise ValueError('平台请求频繁，请稍后再试')
    if not audio.get('url'):
        raise ValueError('这首歌需要会员或暂不可下载' if audio.get('error')
                         else '这首歌暂时无法试听或下载')
    url = audio['url']
    if not _valid_audio(url):
        raise ValueError('未取得有效的官方音频地址')
    return audio


def download(record, directory, progress_path=None):
    file_hash = record.get('hash') or record['id']
    audio = resolve_audio(file_hash)
    extension = (audio.get('extName') or urlsplit(audio['url']).path.rpartition('.')[2]).lower()
    if extension not in AUDIO_EXTENSIONS:
        extension = 'mp3'
    path = Path(directory) / ('audio.' + extension)
    from audio_transfer import download as transfer
    transfer(audio['url'], path, FETCH_HEADERS, progress_path)
    return audio.get('bitRate') or None
