"""Kuwo public search and standard MP3 endpoints, also used by LX sources.

Interface reference: pdone/lx-music-source, xinghai/latest.js (kwTryChannel).
No third-party source scripts are executed.
"""
import ast
import datetime
import html
from pathlib import Path
import re

import requests

HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.kuwo.cn/'}


def clean_text(value):
    return html.unescape(value.replace(r'\u0026', '&')).replace('\xa0', ' ').strip()


def search(keyword):
    response = requests.get('https://search.kuwo.cn/r.s',
                            params={'all': keyword, 'ft': 'music', 'pn': 0, 'rn': 8,
                                    'rformat': 'json', 'encoding': 'utf8'},
                            headers=HEADERS, timeout=8)
    response.raise_for_status()
    if len(response.content) > 2 * 1024 * 1024:
        raise ValueError('搜索响应异常')
    try:
        data = response.json()
    except ValueError:
        # This endpoint also returns dictionaries with single-quoted strings.
        data = ast.literal_eval(response.text)
    songs = []
    for item in data.get('abslist', []):
        match = re.fullmatch(r'MUSIC_(\d+)', item.get('MUSICRID', ''))
        if not match:
            continue
        paid = str(item.get('PAY', '0')) not in ('0', '')
        available = not paid and str(item.get('ONLINE', '1')) == '1'
        songs.append({'source': 'kuwo', 'id': match[1],
                      'title': clean_text(item.get('NAME', '')),
                      'singer': clean_text(item.get('ARTIST', '')),
                      'album': clean_text(item.get('ALBUM', '')),
                      'duration': str(datetime.timedelta(seconds=int(item.get('DURATION') or 0))),
                      'hash': '', 'playable': available, 'downloadable': available,
                      'availability': '需会员' if paid else '' if available else '暂不可用'})
    return songs


def resolve_audio(song_id):
    response = requests.get('https://mobi.kuwo.cn/mobi.s',
                            params={'user': '0', 'source': 'kwplayer_ar_8.5.5.0_apk_keluze.apk',
                                    'type': 'convert_url_with_sign', 'br': '128kmp3',
                                    'format': 'mp3', 'sig': '0', 'rid': song_id,
                                    'network': 'WIFI', 'f': 'web'},
                            headers=HEADERS, timeout=8)
    response.raise_for_status()
    data = response.json()
    audio = data.get('data') or {}
    if data.get('code') != 200 or not audio.get('url') or audio.get('format') not in ('mp3', 'aac', 'm4a'):
        raise ValueError('未取得可播放的标准音频')
    return audio


def download(record, directory, progress_path=None):
    if record.get('playable') is False:
        raise ValueError('这首歌暂时无法试听或下载')
    audio = resolve_audio(record['id'])
    name = re.sub(r'[\\/:*?"<>|]', '', f"{record['singer']} - {record['title']}").strip()[:70]
    path = Path(directory) / ((name or 'audio') + '.mp3')
    from audio_transfer import download as transfer
    transfer(audio['url'], path, HEADERS, progress_path)
    return audio.get('bitrate') or None
