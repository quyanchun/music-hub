"""NAS live API test: off-chart discovery, exclusions, stable IDs and real audio."""
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

from catalog import Catalog
from resolver import blocked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8766')
    parser.add_argument('--data', default='/experiment/data')
    parser.add_argument('--report', default='/experiment/data/search-verification.json')
    args = parser.parse_args()
    catalog = Catalog(args.data)
    account = json.loads((Path(args.data) / 'credentials.json').read_text())
    report = {'version': '0.1.2', 'queries': [], 'audio': [], 'checks': {}}
    _, boards = catalog.visible()
    chart_ids = {key for board in boards for key in json.loads(board['members'])}

    def request(endpoint, extra=None, headers=None):
        salt = secrets.token_hex(8)
        params = {'u': account['username'], 't': hashlib.md5((account['password'] + salt).encode()).hexdigest(),
            's': salt, 'v': '1.16.1', 'c': 'online-search-verifier', 'f': 'json', **(extra or {})}
        req = Request(args.base + '/rest/' + endpoint + '.view?' + urlencode(params), headers=headers or {})
        with urlopen(req, timeout=60) as response:
            return response.status, dict(response.headers), response.read()

    def api(endpoint, extra=None):
        _, _, body = request(endpoint, extra)
        result = json.loads(body)['subsonic-response']
        assert result['status'] == 'ok', (endpoint, result.get('error'))
        return result

    report['version'] = api('ping')['serverVersion']
    assert tuple(map(int, report['version'].split('.'))) >= (0, 1, 2)
    all_found = {}
    for query in ('旅行的意义', '夜空中最亮的星', '南山南'):
        params = {'query': query, 'songCount': 100, 'albumCount': 0, 'artistCount': 0}
        before = time.monotonic()
        songs = api('search3', params)['searchResult3']['song']
        elapsed = round(time.monotonic() - before, 3)
        before = time.monotonic()
        repeat = api('search3', params)['searchResult3']['song']
        cached = round(time.monotonic() - before, 3)
        assert [s['id'] for s in songs] == [s['id'] for s in repeat]
        paged = []
        for offset in range(0, len(songs), 3):
            paged += api('search2', {**params, 'songCount': 3, 'songOffset': offset})['searchResult2']['song']
        assert [s['id'] for s in songs] == [s['id'] for s in paged]
        off_chart = [s for s in songs if s['id'] not in chart_ids]
        for song in songs:
            assert not blocked(catalog.song(song['id']))
            assert api('getSong', {'id': song['id']})['song']['id'] == song['id']
        all_found.update({s['id']: s for s in off_chart})
        item = {'query': query, 'songs': len(songs), 'off_chart': len(off_chart),
                'sources': sorted({s['genre'] for s in songs}),
                'first_seconds': elapsed, 'cached_seconds': cached, 'pagination_stable': True}
        report['queries'].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    assert all_found, 'No off-chart tracks discovered'
    # Test at least one off-chart result from each available source, up to five.
    sources = sorted({s['genre'] for s in all_found.values()})
    for source in sources:
        failures = []
        for song in [s for s in all_found.values() if s['genre'] == source][:3]:
            try:
                status, headers, head = request('stream', {'id': song['id']}, {'Range': 'bytes=0-1023'})
                assert status == 206 and len(head) == 1024
                status, _, content = request('stream', {'id': song['id']})
                assert status == 200 and len(content) > 16384
                with tempfile.TemporaryDirectory() as folder:
                    file = Path(folder) / ('audio.' + song['suffix'])
                    file.write_bytes(content)
                    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'json', str(file)], capture_output=True, text=True, check=True, timeout=12)
                    duration = float(json.loads(result.stdout)['format']['duration'])
                    assert abs(duration - song['duration']) <= max(8, song['duration'] * .03)
                    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(file), '-t', '5', '-f', 'null', '-'],
                                   capture_output=True, check=True, timeout=20)
                status, _, tail = request('stream', {'id': song['id']}, {'Range': 'bytes=-1024'})
                assert status == 206 and tail == content[-1024:]
                item = {'source': source, 'title': song['title'], 'off_chart': True,
                    'bytes': len(content), 'duration': round(duration, 2), 'range_seek_decode': True}
                report['audio'].append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
                break
            except Exception as error:
                failures.append(type(error).__name__)
        else:
            raise AssertionError((source, failures))
    with catalog.connect() as db:
        searches = [json.loads(r['stats']) for r in db.execute('SELECT stats FROM searches')]
        verified = db.execute("SELECT media FROM songs WHERE status='accepted'").fetchall()
    report['search_stats'] = searches
    report['checks'] = {'off_chart_search_and_playback': bool(report['audio']),
        'all_discovered_sources_played': len(report['audio']) == len(sources),
        'stable_pagination_and_cache': all(q['pagination_stable'] for q in report['queries']),
        'five_sources_searched': any(len(s['sources']) == 5 for s in searches),
        'membership_excluded': sum(s['blocked'] for s in searches) > 0,
        'duration_verified': any(json.loads(r['media']).get('duration_verified') for r in verified)}
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    assert all(report['checks'].values()), report['checks']
    print(json.dumps(report['checks'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
