"""Real Subsonic recommendation rotation, public discovery and audio smoke test."""
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from catalog import Catalog
from discovery import identity
from resolver import blocked


def main():
    catalog = Catalog('/experiment/data')
    account = json.loads((catalog.folder / 'credentials.json').read_text())
    report = {'version': '0.1.3', 'batches': [], 'audio': [], 'checks': {}}

    def request(endpoint, values=None, headers=None):
        salt = secrets.token_hex(8)
        params = {'u': account['username'], 't': hashlib.md5((account['password'] + salt).encode()).hexdigest(),
                  's': salt, 'v': '1.16.1', 'c': 'discovery-verifier', 'f': 'json', **(values or {})}
        req = Request('http://127.0.0.1:8766/rest/' + endpoint + '.view?' + urlencode(params), headers=headers or {})
        with urlopen(req, timeout=60) as response:
            return response.status, dict(response.headers), response.read()

    def api(endpoint, params=None):
        _, _, raw = request(endpoint, params)
        body = json.loads(raw)['subsonic-response']
        assert body['status'] == 'ok', (endpoint, body.get('error'))
        return body

    report['version'] = api('ping')['serverVersion']
    assert tuple(map(int, report['version'].split('.'))) >= (0, 1, 3)
    deadline = time.monotonic() + 240
    while True:
        state = catalog.status().get('discovery', {})
        if state.get('finished') and not state.get('running'):
            break
        assert time.monotonic() < deadline, state
        time.sleep(2)
    assert not state.get('error'), state
    report['discovery'] = state
    _, boards = catalog.visible()
    chart_ids = {key for b in boards for key in json.loads(b['members'])}
    with catalog.connect() as db:
        pool = {r['id'] for r in db.execute('SELECT id FROM discoveries')}
    assert pool and not pool & chart_ids
    batches = []
    for n in range(2):
        before = time.monotonic()
        songs = api('getRandomSongs', {'size': 50})['randomSongs']['song']
        elapsed = round(time.monotonic() - before, 3)
        assert len(songs) == 50
        assert len({identity(s) for s in songs}) == 50
        for song in songs:
            assert not blocked(catalog.song(song['id']))
        item = {'number': n + 1, 'songs': len(songs), 'discovered': sum(s['id'] in pool for s in songs),
                'off_chart': sum(s['id'] not in chart_ids for s in songs), 'seconds': elapsed,
                'sources': sorted({s['genre'] for s in songs}), 'artists': len({s['artist'] for s in songs})}
        report['batches'].append(item)
        batches.append(songs)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    overlap = {identity(s) for s in batches[0]} & {identity(s) for s in batches[1]}
    report['overlap'] = len(overlap)
    assert not overlap
    candidates = [s for s in batches[0] + batches[1] if s['id'] in pool]
    assert candidates, 'No newly discovered songs returned'
    failures = []
    for song in candidates[:6]:
        try:
            assert api('getSong', {'id': song['id']})['song']['id'] == song['id']
            status, _, head = request('stream', {'id': song['id']}, {'Range': 'bytes=0-1023'})
            assert status == 206 and len(head) == 1024
            status, _, audio = request('stream', {'id': song['id']})
            assert status == 200
            with tempfile.TemporaryDirectory() as directory:
                file = Path(directory) / ('audio.' + song['suffix'])
                file.write_bytes(audio)
                result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                    '-of', 'json', str(file)], check=True, capture_output=True, text=True, timeout=12)
                duration = float(json.loads(result.stdout)['format']['duration'])
                assert abs(duration - song['duration']) <= max(8, song['duration'] * .03)
                subprocess.run(['ffmpeg', '-v', 'error', '-i', str(file), '-t', '5', '-f', 'null', '-'],
                               check=True, capture_output=True, timeout=20)
            status, _, tail = request('stream', {'id': song['id']}, {'Range': 'bytes=-1024'})
            assert status == 206 and tail == audio[-1024:]
            item = {'title': song['title'], 'artist': song['artist'], 'source': song['genre'],
                    'duration': round(duration, 2), 'bytes': len(audio), 'range_seek_decode': True}
            report['audio'].append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            if len(report['audio']) >= 3:
                break
        except Exception as error:
            failures.append(type(error).__name__)
    report['checks'] = {'public_discovery_expanded': state['new_tracks'] > 0,
        'both_batches_fifty': all(len(b) == 50 for b in batches),
        'no_cross_source_recording_duplicates': all(len({identity(s) for s in b}) == 50 for b in batches),
        'no_repeats_between_batches': not overlap, 'off_chart_recommendations': bool(candidates),
        'fast_cached_recommendation': all(b['seconds'] < 2 for b in report['batches']),
        'discovered_audio_verified': bool(report['audio'])}
    report['audio_failures'] = failures
    (catalog.folder / 'discovery-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['checks'], ensure_ascii=False), flush=True)
    assert all(report['checks'].values()), report['checks']


if __name__ == '__main__':
    main()
