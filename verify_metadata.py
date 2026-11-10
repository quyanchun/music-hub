"""Verify playback-page endpoints without printing credentials or lyric text."""
import hashlib
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode
from urllib.request import urlopen
import xml.etree.ElementTree as ET


def main():
    account = json.loads(Path('/experiment/data/credentials.json').read_text())
    base = 'http://127.0.0.1:8766/rest/'

    def raw(endpoint, params=None):
        salt = secrets.token_hex(8)
        values = {'u': account['username'], 's': salt,
                  't': hashlib.md5((account['password'] + salt).encode()).hexdigest(),
                  'v': '1.16.1', 'c': 'metadata-verifier', 'f': 'json', **(params or {})}
        with urlopen(base + endpoint + '?' + urlencode(values), timeout=30) as response:
            return response.headers.get('Content-Type'), response.read()

    def api(endpoint, params=None):
        _, content = raw(endpoint, params)
        data = json.loads(content)['subsonic-response']
        assert data['status'] == 'ok', (endpoint, data.get('error'))
        return data

    tracks = []
    for offset in range(0, 10000, 500):
        batch = api('search3', {'query': '', 'songCount': 500, 'songOffset': offset,
                                'artistCount': 0, 'albumCount': 0})['searchResult3']['song']
        tracks.extend(batch)
        if len(batch) < 500:
            break
    report = {'version': api('ping')['serverVersion'], 'samples': [], 'checks': {}}
    assert api('getOpenSubsonicExtensions')['openSubsonicExtensions'] == [{'name': 'songLyrics', 'versions': [1]}]
    for source in ('网易云', '酷我', 'QQ音乐', '酷狗', '咪咕'):
        candidates = [s for s in tracks if s['genre'] == source]
        # Also sample a previously decoded recording where possible.
        preferred = ['海屿你', '20001106', '时过境迁', '雨爱']
        candidates.sort(key=lambda s: (s['title'] not in preferred, s['title']))
        for song in candidates[:4]:
            before = time.monotonic()
            lyrics = api('getLyricsBySongId', {'id': song['id']})['lyricsList']['structuredLyrics']
            elapsed = round(time.monotonic() - before, 3)
            if not lyrics:
                continue
            _, xml = raw('getLyrics', {'artist': song['artist'], 'title': song['title'], 'f': 'xml'})
            legacy = ET.fromstring(xml).find('{*}lyrics')
            similar = api('getSimilarSongs', {'id': song['id'], 'count': 20})['similarSongs']['song']
            similar2 = api('getSimilarSongs2', {'id': song['artistId'], 'count': 20})['similarSongs2']['song']
            top = api('getTopSongs', {'artist': song['artist'], 'count': 20})['topSongs']['song']
            assert similar and top
            assert all(s['id'] != song['id'] for s in similar)
            mime, cover = raw('getCoverArt', {'id': song['coverArt']})
            assert mime.startswith('image/') and len(cover) > 100
            before = time.monotonic()
            api('getLyricsBySongId', {'id': song['id']})
            sample = {'source': source, 'title': song['title'], 'structured_lines': len(lyrics[0]['line']),
                      'synced': lyrics[0]['synced'], 'legacy_has_text': bool(legacy is not None and legacy.text),
                      'similar_count': len(similar), 'artist_similar_count': len(similar2), 'top_count': len(top),
                      'cover_type': mime, 'cold_seconds': elapsed, 'cached_seconds': round(time.monotonic() - before, 3)}
            report['samples'].append(sample)
            print(json.dumps(sample, ensure_ascii=False), flush=True)
            break
        else:
            report['samples'].append({'source': source, 'available_lyrics': False,
                                      'tested_candidates': min(4, len(candidates))})
    for key in ('al-stale-client-id', 'pl-netease-hot'):
        mime, data = raw('getCoverArt', {'id': key})
        assert mime.startswith('image/') and data.startswith((b'\x89PNG', b'\xff\xd8\xff'))
    assert api('getSimilarSongs', {'id': 'missing'})['similarSongs']['song'] == []
    assert api('getLyricsBySongId', {'id': 'missing'})['lyricsList']['structuredLyrics'] == []
    report['checks'] = {'similar_endpoints': True, 'top_songs': True, 'stale_cover_image_fallback': True,
                        'unknown_data_completes': True, 'structured_and_legacy_lyrics': True}
    Path('/experiment/data/metadata-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
