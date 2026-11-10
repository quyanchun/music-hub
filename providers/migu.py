"""Migu Music search and playback through its public mobile endpoints.

This adapter revives the 2019 addon bundled in vendor/music_dl/addons/migu.py.
Earlier rounds dismissed Migu after testing the newer listen-url/v2.x API,
resourceinfo.do and third-party relays; those still fail. The old
`listenSong.do` endpoint, however, was re-verified end to end on 2026-11-09:
it answers 302 to a freetyst.nf.migu.cn MP3 for non-VIP songs. Two different
contentIds produced two different files, each byte-for-byte matching its
declared PQ size, and a fake contentId returned a 72-byte error JSON.

The server ignores toneFlag entirely (LQ/PQ/HQ/SQ all resolve to the same
MP3_128_16_Stero stream), so this adapter only ever requests standard 128 kbps
quality. Songs whose PQ rate carries a `vip` showTag are marked as requiring a
membership upfront, because listenSong.do returns 200 without a redirect for
them. No login state, cookie or third-party relay is involved.
"""
import datetime
import html
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

SEARCH_API = 'http://pd.musicapp.migu.cn/MIGUM2.0/v1.0/content/search_all.do'
LISTEN_API = 'http://app.pd.nf.migu.cn/MIGUM2.0/v1.0/content/sub/listenSong.do'
CHART_API = 'https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/querycontentbyId.do'
USER_ID = '15548614588710179085069'
IOS_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 12_0 like Mac OS X) '
          'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/12.0 Mobile/15E148 Safari/604.1')
ANDROID_UA = ('Mozilla/5.0 (Linux; Android 5.1.1; Nexus 6 Build/LYZ28E) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/59.0.3071.115 Mobile Safari/537.36')
SEARCH_HEADERS = {'User-Agent': IOS_UA, 'referer': 'http://music.migu.cn/'}
LISTEN_HEADERS = {'User-Agent': ANDROID_UA, 'Referer': 'http://music.migu.cn/'}
CHART_HEADERS = {'User-Agent': ANDROID_UA, 'Referer': 'https://app.c.nf.migu.cn/'}
SEARCH_SWITCH = ('{"song":1,"album":0,"singer":0,"tagSong":0,'
                 '"mvSong":0,"songlist":0,"bestShow":1}')
# bangid values used by LX Music's musicSdk/mg/leaderboard.js.
BOARDS = {'hot': '27186466', 'new': '27553319', 'rising': '75959118'}
BOARD_NAMES = {'hot': '尖叫热歌榜', 'new': '尖叫新歌榜', 'rising': '音乐风向榜'}
CONTENT_ID = re.compile(r'\d{10,24}')


def clean_text(value):
    return html.unescape(str(value or '')).replace('\xa0', ' ').strip()


def pq_rate(item):
    rates = item.get('rateFormats') or []
    return next((r for r in rates if r.get('formatType') == 'PQ'), None)


def is_vip(item):
    """PQ showTag 'vip' predicts a 200-without-redirect from listenSong.do.

    Verified against 12 real tracks on 2026-11-09: the flag and the missing
    redirect agreed in every case. Top-level vipType is NOT reliable (some
    tracks carry it while still resolving a free stream).
    """
    pq = pq_rate(item)
    if pq is not None:
        return 'vip' in (pq.get('showTag') or [])
    return (any('vip' in (r.get('showTag') or []) for r in item.get('rateFormats') or [])
            or 'vip' in (item.get('showTag') or []))


def pq_size(item):
    rate = pq_rate(item)
    value = rate.get('size') if rate else None
    return int(value) if str(value or '').isdigit() else 0


def estimated_seconds(item):
    """The 128 kbps stream is CBR, so declared size gives an accurate duration."""
    size = pq_size(item)
    return round(size * 8 / 128000) if size else 0


def validate_content_id(value):
    content_id = str(value or '')
    if not CONTENT_ID.fullmatch(content_id):
        raise ValueError('歌曲标识无效')
    return content_id


def search_params(keyword, size=30):
    return {'ua': 'Android_migu', 'version': '5.0.1', 'text': keyword,
            'pageNo': 1, 'pageSize': size, 'searchSwitch': SEARCH_SWITCH}


def search(keyword):
    response = requests.get(SEARCH_API, params=search_params(keyword),
                            headers=SEARCH_HEADERS, timeout=10)
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError('搜索响应异常')
    items = ((response.json().get('songResultData') or {}).get('result') or [])
    # Surface freely playable tracks first; VIP tracks cannot be resolved.
    ordered = [i for i in items if not is_vip(i)] + [i for i in items if is_vip(i)]
    songs = []
    for item in ordered:
        content_id = str(item.get('contentId') or '')
        if not CONTENT_ID.fullmatch(content_id):
            continue
        seconds = estimated_seconds(item)
        singers = '、'.join(clean_text(s.get('name')) for s in item.get('singers') or []
                           if s.get('name'))
        albums = item.get('albums') or []
        album = clean_text(albums[0].get('name')) if albums else ''
        vip = is_vip(item)
        songs.append({'source': 'migu', 'id': content_id,
                      'title': clean_text(item.get('name')), 'singer': singers,
                      'album': album, 'duration': str(datetime.timedelta(seconds=seconds)),
                      'duration_seconds': seconds, 'hash': '',
                      'playable': not vip, 'downloadable': not vip,
                      'availability': '需会员' if vip else ''})
        if len(songs) >= 8:
            break
    return songs


def chart_tracks(board):
    response = requests.get(CHART_API, params={'columnId': board, 'needAll': 0},
                            headers=CHART_HEADERS, timeout=12)
    response.raise_for_status()
    if len(response.content) > 4 * 1024 * 1024:
        raise ValueError('榜单响应异常')
    body = response.json()
    if body.get('code') != '000000':
        raise ValueError('榜单暂时不可用')
    contents = (body.get('columnInfo') or {}).get('contents') or []
    return [c.get('objectInfo') or {} for c in contents]


def chart_song(item, rank):
    length = str(item.get('length') or '')
    parts = length.split(':')
    seconds = 0
    if 1 <= len(parts) <= 3 and all(p.isdigit() for p in parts):
        for part in parts:
            seconds = seconds * 60 + int(part)
    if not seconds:
        seconds = estimated_seconds(item)
    vip = is_vip(item)
    return {'source': 'migu', 'id': str(item.get('contentId') or ''),
            'title': clean_text(item.get('songName') or item.get('name')),
            'singer': clean_text(item.get('singer')),
            'album': clean_text(item.get('album')),
            'duration': str(datetime.timedelta(seconds=seconds)),
            'duration_seconds': seconds, 'hash': '', 'rank': rank,
            'playable': not vip, 'downloadable': not vip,
            'availability': '需会员' if vip else ''}


def resolve_audio(content_id):
    content_id = validate_content_id(content_id)
    params = {'toneFlag': 'PQ', 'netType': '00', 'userId': USER_ID,
              'ua': 'Android_migu', 'version': '5.1', 'copyrightId': 0,
              'contentId': content_id, 'resourceType': '2', 'channel': 0}
    response = requests.get(LISTEN_API, params=params, headers=LISTEN_HEADERS,
                            timeout=12, allow_redirects=False)
    location = response.headers.get('Location') if response.status_code in (301, 302, 303, 307, 308) else None
    if not location:
        raise ValueError('这首歌需要咪咕会员或暂不可下载')
    url = urljoin(LISTEN_API, location)
    host = (urlsplit(url).hostname or '').lower()
    if not (host == 'migu.cn' or host.endswith('.migu.cn')):
        raise ValueError('未取得有效的官方音频地址')
    if urlsplit(url).path.lower().split('.')[-1] not in ('mp3', 'm4a', 'flac', 'ape'):
        raise ValueError('未取得有效的官方音频地址')
    return url


def download(record, directory, progress_path=None):
    url = resolve_audio(record.get('id'))
    from audio_transfer import download as transfer
    transfer(url, Path(directory) / 'audio.mp3', LISTEN_HEADERS, progress_path)
    return 128
