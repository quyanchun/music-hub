"""Real-client smoke test. Reads credentials from a private file; never prints them."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8766')
    parser.add_argument('--credentials', default='/experiment/data/credentials.json')
    parser.add_argument('--report', default='/experiment/data/verification.json')
    parser.add_argument('--playback', action='store_true')
    args = parser.parse_args()
    account = json.loads(Path(args.credentials).read_text())
    results = {'server': args.base, 'checks': {}, 'audio': []}

    def request(endpoint, extra=None, headers=None):
        salt = secrets.token_hex(8)
        params = {'u': account['username'], 't': hashlib.md5((account['password'] + salt).encode()).hexdigest(),
                  's': salt, 'v': '1.16.1', 'c': 'experiment-verifier', 'f': 'json', **(extra or {})}
        req = Request(args.base.rstrip('/') + '/rest/' + endpoint + '.view?' + urlencode(params), headers=headers or {})
        with urlopen(req, timeout=100) as response:
            return response.status, dict(response.headers), response.read()

    def api(endpoint, extra=None):
        _, _, raw = request(endpoint, extra)
        body = json.loads(raw)['subsonic-response']
        if body['status'] != 'ok':
            raise RuntimeError((endpoint, body.get('error')))
        return body

    assert api('ping')['version'] == '1.16.1'
    _, _, xml = request('ping', {'f': 'xml'})
    assert ET.fromstring(xml).attrib['status'] == 'ok'
    _, _, failure = request('ping', {'t': 'wrong'})
    assert json.loads(failure)['subsonic-response']['error']['code'] == 40
    results['checks']['json_xml_and_token_auth'] = True
    folders = api('getMusicFolders')['musicFolders']['musicFolder']
    assert len(folders) == 6
    playlists = api('getPlaylists')['playlists']['playlist']
    assert playlists[0]['id'] == 'cached-songs'
    assert all(p['songCount'] > 0 for p in playlists[1:])
    results['playlists'] = [{'name': p['name'], 'songs': p['songCount']} for p in playlists]
    results['checks']['cache_first_and_empty_playlists_hidden'] = True
    all_songs, offset = [], 0
    while True:
        batch = api('search3', {'query': '', 'songCount': 500, 'songOffset': offset,
                                'artistCount': 0, 'albumCount': 0})['searchResult3']['song']
        all_songs.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    assert all_songs and len({s['id'] for s in all_songs}) == len(all_songs)
    scan = api('getScanStatus')['scanStatus']
    # Background discovery can change the count between pagination and this request.
    results['scan_count_at_end'] = scan['count']
    results['songs'] = len(all_songs)
    results['sources'] = {name: sum(s['genre'] == name for s in all_songs) for name in ('网易云', '酷我', 'QQ音乐', '酷狗', '咪咕', 'B站音频区')}
    results['scan'] = scan
    results['checks']['complete_paginated_library'] = True
    sample = all_songs[0]
    assert api('getSong', {'id': sample['id']})['song']['id'] == sample['id']
    assert api('getAlbum', {'id': sample['albumId']})['album']['song']
    assert api('getArtist', {'id': sample['artistId']})['artist']['album']
    assert api('search3', {'query': sample['title'], 'songCount': 50})['searchResult3']['song']
    _, headers, image = request('getCoverArt', {'id': sample['coverArt']})
    assert headers['Content-Type'].startswith('image/') and image.startswith((b'\x89PNG', b'\xff\xd8\xff'))
    results['checks']['browse_search_cover'] = True
    for playlist in playlists:
        entry = api('getPlaylist', {'id': playlist['id']})['playlist']
        assert len(entry['entry']) == entry['songCount']
    if args.playback:
        for name in results['sources']:
            candidates = [s for s in all_songs if s['genre'] == name][:4]
            if not candidates:
                results['audio'].append({'source': name, 'ok': False, 'reason': 'no verified tracks'})
                continue
            attempts = []
            for song in candidates:
                try:
                    before = time.monotonic()
                    status, headers, chunk = request('stream', {'id': song['id']}, {'Range': 'bytes=0-1023'})
                    assert status == 206 and len(chunk) == 1024 and headers['Content-Type'].startswith('audio/')
                    first_seconds = round(time.monotonic() - before, 2)
                    status, _, content = request('stream', {'id': song['id']})
                    assert status == 200 and len(content) > 16384
                    with tempfile.TemporaryDirectory() as folder:
                        file = Path(folder) / ('audio.' + song['suffix'])
                        file.write_bytes(content)
                        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                                '-of', 'json', str(file)], capture_output=True, text=True, check=True, timeout=15)
                        duration = float(json.loads(probe.stdout)['format']['duration'])
                        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(file), '-t', '5', '-f', 'null', '-'],
                                       capture_output=True, check=True, timeout=20)
                    status, _, tail = request('stream', {'id': song['id']}, {'Range': 'bytes=-1024'})
                    assert status == 206 and tail == content[-1024:]
                    item = {'source': name, 'title': song['title'], 'ok': True, 'bytes': len(content),
                            'duration': duration, 'first_request_seconds': first_seconds,
                            'range_and_seek': True, 'ffmpeg_decode': True}
                    results['audio'].append(item)
                    print(json.dumps(item, ensure_ascii=False), flush=True)
                    break
                except Exception as error:
                    attempts.append(type(error).__name__)
            else:
                results['audio'].append({'source': name, 'ok': False, 'attempts': attempts})
        results['checks']['audio_all_six_sources'] = all(a['ok'] for a in results['audio']) and len(results['audio']) == 6
    Path(args.report).write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    if not all(results['checks'].values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
