"""Run on the NAS host (outside measured container); never print credentials."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--container', required=True)
    parser.add_argument('--base', default='http://127.0.0.1:18766')
    parser.add_argument('--report', required=True)
    parser.add_argument('--scan', action='store_true')
    args = parser.parse_args()
    ready_until = time.monotonic() + 15
    while True:
        try:
            with urlopen(args.base + '/health', timeout=1) as response:
                assert response.status == 200
            break
        except OSError:
            if time.monotonic() >= ready_until:
                raise
            time.sleep(.1)
    inspect = json.loads(docker('inspect', args.container))[0]
    pid = inspect['State']['Pid']
    group = next(line.split('::', 1)[1] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines() if line.startswith('0::'))
    root = Path('/sys/fs/cgroup') / group.lstrip('/')
    account = json.loads(docker('exec', args.container, 'cat', '/experiment/data/credentials.json'))
    report = {'container': args.container, 'image': inspect['Config']['Image'],
              'memory_limit': inspect['HostConfig']['Memory'], 'swap_limit': inspect['HostConfig']['MemorySwap'],
              'measurements': {}, 'checks': {}, 'search': [], 'audio': []}
    stop = threading.Event()
    lock = threading.Lock()
    maximum = {'charged': 0, 'working': 0, 'anon': 0, 'processes': 0}

    def sample():
        values = dict(line.split() for line in (root / 'memory.stat').read_text().splitlines())
        current = int((root / 'memory.current').read_text())
        values = {k: int(v) for k, v in values.items()}
        return {'charged': current, 'working': max(0, current - values.get('inactive_file', 0)),
                'anon': values.get('anon', 0), 'processes': len((root / 'cgroup.procs').read_text().splitlines())}

    def events():
        return {k: int(v) for k, v in (line.split() for line in (root / 'memory.events').read_text().splitlines())}

    report['measurements']['before'] = sample()
    before_events = events()
    def monitor():
        while not stop.is_set():
            try:
                values = sample()
                with lock:
                    for key in maximum:
                        maximum[key] = max(maximum[key], values[key])
            except OSError:
                pass
            stop.wait(.04)
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()

    def request(endpoint, extra=None, headers=None, destination=None):
        salt = secrets.token_hex(8)
        params = {'u': account['username'], 't': hashlib.md5((account['password'] + salt).encode()).hexdigest(),
                  's': salt, 'v': '1.16.1', 'c': 'memory-benchmark', 'f': 'json', **(extra or {})}
        req = Request(args.base + '/rest/' + endpoint + '.view?' + urlencode(params), headers=headers or {})
        with urlopen(req, timeout=90) as response:
            if destination:
                with destination.open('wb') as file:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        file.write(chunk)
                raw = b''
            else:
                raw = response.read()
            return response.status, dict(response.headers), raw

    def api(endpoint, extra=None):
        _, _, raw = request(endpoint, extra)
        body = json.loads(raw)['subsonic-response']
        assert body['status'] == 'ok', (endpoint, body.get('error'))
        return body

    def search(query):
        before = time.monotonic()
        found = api('search3', {'query': query, 'songCount': 100, 'albumCount': 0, 'artistCount': 0})['searchResult3']['song']
        assert found, 'No results: ' + query
        return {'query': query, 'songs': len(found), 'seconds': round(time.monotonic() - before, 2)}

    def play(song):
        with tempfile.TemporaryDirectory(prefix='music-memory-') as folder:
            path = Path(folder) / 'audio'
            status, _, _ = request('stream', {'id': song['id']}, destination=path)
            assert status == 200 and path.stat().st_size > 16384
            status, _, tail = request('stream', {'id': song['id']}, {'Range': 'bytes=-1024'})
            with path.open('rb') as file:
                file.seek(-1024, 2)
                assert status == 206 and tail == file.read()
            # Client-side decode runs in a separate tools container, outside the
            # measured service. Audio is streamed to disk, not held in its RAM.
            subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--user', '0',
                '-v', folder + ':/samples:ro', '--entrypoint', 'ffmpeg', inspect['Config']['Image'],
                '-v', 'error', '-i', '/samples/audio', '-t', '3', '-f', 'null', '-'],
                capture_output=True, check=True, timeout=30)
            return {'source': song['genre'], 'title': song['title'], 'bytes': path.stat().st_size,
                    'range_seek_decode': True}

    def play_source(candidates):
        attempts = []
        for song in candidates:
            try:
                item = play(song)
                item['failed_attempts'] = attempts
                return item
            except Exception as exc:
                attempts.append(type(exc).__name__)
        raise AssertionError((candidates[0]['genre'], attempts))

    error = None
    try:
        report['version'] = api('ping')['serverVersion']
        songs = []
        for offset in range(0, 10000, 500):
            batch = api('search3', {'query': '', 'songCount': 500, 'songOffset': offset, 'albumCount': 0, 'artistCount': 0})['searchResult3']['song']
            songs.extend(batch)
            if len(batch) < 500:
                break
        assert songs
        report['songs'] = len(songs)
        report['checks']['library_and_auth'] = True
        recommendations = [api('getRandomSongs', {'size': 50})['randomSongs']['song'] for _ in range(2)]
        assert all(len(b) == 50 for b in recommendations)
        assert not {s['id'] for s in recommendations[0]} & {s['id'] for s in recommendations[1]}
        report['checks']['recommendation_rotation'] = True
        picks = [next(s for s in songs if s['genre'] == source) for source in ('网易云', '酷我', 'QQ音乐', '酷狗', '咪咕')]
        # Exercise two cold searches, concurrent library browsing and playback.
        with ThreadPoolExecutor(max_workers=10) as pool:
            searches = [pool.submit(search, q) for q in ('稻香', '平凡之路')]
            browsing = [pool.submit(api, 'getArtists'), pool.submit(api, 'getPlaylists'),
                        pool.submit(api, 'getAlbumList2', {'type': 'random', 'size': 100}),
                        pool.submit(api, 'search3', {'query': '', 'songCount': 500})]
            audio = [pool.submit(play_source, [s for s in songs if s['genre'] == sample['genre']][:3]) for sample in picks]
            for future in searches:
                item = future.result()
                report['search'].append(item)
                print(json.dumps({'search': item}, ensure_ascii=False), flush=True)
            for future in browsing:
                future.result()
            for future in audio:
                item = future.result()
                report['audio'].append(item)
                print(json.dumps({'audio': item}, ensure_ascii=False), flush=True)
        report['checks']['online_search_and_parallel_browse'] = True
        report['checks']['five_source_playback'] = len(report['audio']) == 5
        for song in picks:
            api('getSong', {'id': song['id']})
            api('getSimilarSongs2', {'id': song['id'], 'count': 20})
            api('getTopSongs', {'artist': song['artist']})
            api('getLyricsBySongId', {'id': song['id']})
            _, headers, body = request('getCoverArt', {'id': song['coverArt']})
            assert headers['Content-Type'].startswith('image/') and body
        report['checks']['lyrics_cover_similar_top'] = True
        if args.scan:
            api('startScan')
            deadline = time.monotonic() + 30
            count = 0
            while time.monotonic() < deadline:
                api('getAlbumList2', {'type': 'random', 'size': 100})
                status = api('getScanStatus')['scanStatus']
                assert len(api('getPlaylists')['playlists']['playlist']) == 15
                count += 1
                time.sleep(1)
            report['scan_observation'] = {'browse_rounds': count, 'still_scanning': status['scanning']}
            report['checks']['scan_while_browsing'] = True
        # Background discovery is forced due in the isolated snapshot, so its
        # public-search/verification overlaps the workload without changing live data.
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            states = []
            for line in docker('logs', args.container).splitlines():
                try:
                    value = json.loads(line)
                    if value.get('event') == 'discovery_refresh':
                        states.append(value)
                except ValueError:
                    pass
            if states:
                report['background_last_attempt'] = states[-1]
                completed = [state for state in states if not state.get('error') and state.get('seeds', 0) > 0]
                assert completed, states[-1]
                report['background_discovery'] = completed[-1]
                report['checks']['background_discovery'] = True
                break
            time.sleep(2)
        else:
            raise AssertionError('Background discovery did not complete')
        time.sleep(2)
    except Exception as exc:
        error = exc
        report['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
    finally:
        stop.set()
        watcher.join(2)
        report['measurements']['sampled_peak'] = maximum
        report['measurements']['after'] = sample()
        report['measurements']['cgroup_peak'] = int((root / 'memory.peak').read_text())
        report['events_delta'] = {k: v - before_events.get(k, 0) for k, v in events().items()}
        state = json.loads(docker('inspect', args.container))[0]['State']
        report['memory_events_total'] = events()
        report['checks']['no_oom_or_restart'] = not state['OOMKilled'] and report['memory_events_total'].get('oom_kill', 0) == 0
        report['checks']['zero_swap'] = int((root / 'memory.swap.current').read_text()) == 0
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'peak': maximum, 'checks': report['checks'], 'error': report.get('error')}, ensure_ascii=False), flush=True)
    if error:
        raise error
    assert all(report['checks'].values()), report['checks']


if __name__ == '__main__':
    main()
