"""Anonymous standard-quality resolution; never use member cookies or trial audio."""
from pathlib import Path
import json
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

import requests

PROVIDERS = Path(__file__).resolve().parent / 'providers'
sys.path[:0] = [str(PROVIDERS), str(PROVIDERS / 'vendor')]
from music_dl import config
config.init()

NAMES = {'netease': '网易云', 'kuwo': '酷我', 'qq': 'QQ音乐', 'kugou': '酷狗', 'migu': '咪咕', 'bilibili': 'B站音频区'}
class AudioInspector:
    def __init__(self):
        self.condition = threading.Condition()
        self.busy = False
        self.players = 0

    @contextmanager
    def slot(self, playback):
        with self.condition:
            if playback:
                self.players += 1
            try:
                while self.busy or (not playback and self.players):
                    self.condition.wait()
                self.busy = True
            finally:
                if playback:
                    self.players -= 1
        try:
            yield
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()


INSPECTOR = AudioInspector()


def inspect_audio(target, headers=None, background=False):
    # Search and playback share this budget; ten FFmpeg processes dwarf the API.
    with INSPECTOR.slot(playback=headers is None and not background):
        command = ['ffprobe', '-v', 'error', '-threads', '1']
        if headers is not None:
            command += ['-rw_timeout', '4000000', '-protocol_whitelist', 'http,https,tcp,tls',
                        '-probesize', '131072', '-analyzeduration', '2000000', '-headers',
                        ''.join(f'{k}: {v}\r\n' for k, v in headers.items())]
        result = subprocess.run(command + ['-show_entries', 'format=duration,bit_rate:stream=codec_type',
            '-of', 'json', str(target)], capture_output=True, text=True, timeout=12 if headers is None else 9, check=True)
        return json.loads(result.stdout)


def blocked(song):
    return (song.get('playable') is False or song.get('downloadable') is False
            or any(word in song.get('availability', '') for word in ('会员', '试听', '付费')))


def seconds(song):
    if song.get('duration_seconds'):
        return int(song['duration_seconds'])
    value = str(song.get('duration', '0')).split(':')
    result = 0
    for part in value:
        result = result * 60 + int(float(part))
    return result


def resolve(song):
    if blocked(song):
        raise ValueError('membership_or_trial')
    source = song['source']
    if source == 'netease':
        from music_dl.addons.netease import NeteaseApi
        body = NeteaseApi.request('https://music.163.com/weapi/song/enhance/player/url',
                                 data=NeteaseApi.encrypted_request({'ids': [song['id']], 'br': 128000}))
        audio = (body.get('data') or [{}])[0]
        if not audio.get('url') or audio.get('freeTrialInfo'):
            raise ValueError('membership_or_trial')
        url, headers = audio['url'], {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://music.163.com/'}
    elif source == 'kuwo':
        import kuwo
        audio = kuwo.resolve_audio(song['id'])
        url, headers = audio['url'], kuwo.HEADERS
    elif source == 'kugou':
        import kugou
        audio = kugou.resolve_audio(song.get('hash') or song['id'])
        if audio.get('freeTrialInfo') or audio.get('is_free_part'):
            raise ValueError('membership_or_trial')
        url, headers = audio['url'], kugou.FETCH_HEADERS
    elif source == 'qq':
        import qq
        url, _ = qq.resolve_audio(song['id'])
        headers = qq.HEADERS
    elif source == 'migu':
        import migu
        url, headers = migu.resolve_audio(song['id']), migu.LISTEN_HEADERS
    elif source == 'bilibili':
        import bilibili
        url, headers = bilibili.resolve_audio(song['id']), bilibili.HEADERS
    else:
        raise ValueError('unsupported_source')
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    domains = {'netease': ('music.126.net', 'music.163.com'), 'kuwo': ('kuwo.cn',),
               'kugou': ('kugou.com',), 'qq': ('qqmusic.qq.com', 'qmusic.qq.com'), 'migu': ('migu.cn',),
               'bilibili': ('bilivideo.com',)}
    if parsed.scheme not in ('http', 'https') or parsed.username or not any(host == d or host.endswith('.' + d) for d in domains[source]):
        raise ValueError('unexpected_audio_host')
    suffix = parsed.path.rsplit('.', 1)[-1].lower()
    if source == 'kuwo' and audio.get('format') in ('mp3', 'aac', 'm4a'):
        suffix = audio['format']
    if suffix not in ('mp3', 'aac', 'm4a', 'ogg', 'flac', 'wav', 'ape'):
        suffix = 'm4a' if source == 'bilibili' else 'mp3'
    return url, {**headers, 'Accept-Encoding': 'identity'}, suffix


def probe(song, verify_duration=False):
    """Read a small real audio range, and reject obvious short trial responses."""
    url, headers, suffix = resolve(song)
    with requests.get(url, headers={**headers, 'Range': 'bytes=0-4095'},
                      stream=True, timeout=(8, 12)) as response:
        response.raise_for_status()
        chunk = response.raw.read(4096)
        content_range = re.search(r'/(\d+)$', response.headers.get('Content-Range', ''))
        size = int(content_range[1]) if content_range else int(response.headers.get('Content-Length') or 0)
        known = (chunk.startswith((b'ID3', b'fLaC', b'OggS', b'MAC ', b'RIFF'))
                 or chunk[4:8] == b'ftyp'
                 or any(chunk[i] == 255 and chunk[i + 1] & 224 == 224 for i in range(len(chunk) - 1)))
        if not known or size < 16384:
            raise ValueError('not_audio_or_missing_length')
        # All adapters request standard quality. This also filters short previews
        # whose metadata forgot to label them as a trial. Full duration is checked
        # again with ffprobe before the first playback is served.
        if suffix == 'mp3' and seconds(song) > 60 and size < seconds(song) * 10000:
            raise ValueError('suspected_short_preview')
        media = {'suffix': suffix, 'size': size, 'duration': seconds(song), 'bitrate': 128}
    if verify_duration:
        # Inspect the actual CDN audio, including its container duration. A URL
        # and a large response alone do not prove that it is a complete track.
        info = inspect_audio(url, headers)
        duration = float(info.get('format', {}).get('duration') or 0)
        expected = seconds(song)
        if (expected <= 0 or duration <= 0 or abs(expected - duration) > max(8, expected * .03)
                or not any(s.get('codec_type') == 'audio' for s in info.get('streams', []))):
            raise ValueError('short_preview_or_duration_mismatch')
        media.update(duration=round(duration), duration_verified=True,
                     bitrate=round(int(info['format'].get('bit_rate') or 128000) / 1000))
    return media
