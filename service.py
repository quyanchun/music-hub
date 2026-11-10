"""A small, authenticated Subsonic 1.16.1 experiment for chart music clients."""
import argparse
from collections import defaultdict
import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import string
import struct
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit
import xml.etree.ElementTree as ET
import zlib

from catalog import Catalog
from discovery import Discovery
from library import FOLDERS, Library, matches
from media import MediaCache
from maintenance import Maintenance
from metadata import MetadataCache, structured_lyrics
from online_search import OnlineSearch
from public_playlists import PublicPlaylists
from resolver import NAMES


class APIError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def credentials(folder):
    path = Path(folder) / 'credentials.json'
    if not path.exists():
        account = {'username': 'admin', 'password': ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))}
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            json.dump(account, output)
    return json.loads(path.read_text())


def authenticated(params, account):
    user = params.get('u', '')
    if not hmac.compare_digest(user.encode(), account['username'].encode()):
        return False
    if 't' in params and params.get('s'):
        expected = hashlib.md5((account['password'] + params['s']).encode()).hexdigest()
        return hmac.compare_digest(params['t'].encode(), expected.encode())
    password = params.get('p', '')
    if password.startswith('enc:'):
        try:
            password = bytes.fromhex(password[4:]).decode()
        except (ValueError, UnicodeDecodeError):
            return False
    return hmac.compare_digest(password.encode(), account['password'].encode())


def xml_response(body):
    root = ET.Element('subsonic-response', {'xmlns': 'http://subsonic.org/restapi'})

    def add(parent, key, value):
        if isinstance(value, list):
            for item in value:
                add(parent, key, item)
        elif isinstance(value, dict):
            child = ET.SubElement(parent, key)
            fill(child, value)
        elif value is not None:
            ET.SubElement(parent, key).text = str(value)

    def fill(element, values):
        for key, value in values.items():
            if isinstance(value, (dict, list)):
                add(element, key, value)
            elif value is not None:
                if key == 'value':
                    element.text = str(value)
                else:
                    element.set(key, str(value).lower() if isinstance(value, bool) else str(value))
    fill(root, body)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def cover_png(seed):
    digest = hashlib.sha256(seed.encode()).digest()
    size = 96
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            bar = any(abs(x - center) < 4 and abs(y - 48) < height for center, height in ((30, 12), (48, 27), (66, 18)))
            rows.extend((225, 238, 207) if bar else tuple(25 + c % 45 for c in digest[:3]))

    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', size, size, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')


class App(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, folder):
        self.catalog = Catalog(folder)
        self.account = credentials(self.catalog.folder)
        self.media = MediaCache(self.catalog)
        self.metadata = MetadataCache(self.catalog)
        self.search = OnlineSearch(self.catalog)
        self.discovery = Discovery(self.catalog, self.search)
        self.public_playlists = PublicPlaylists(self.catalog)
        self.maintenance = Maintenance(self.catalog, self.media, self.metadata)
        self.stop_event = threading.Event()
        self.view_slots = threading.BoundedSemaphore(2)
        super().__init__(address, Handler)

    def server_close(self):
        self.search.close()
        super().server_close()

    def refresh_loop(self):
        finished = self.catalog.status().get('scan', {}).get('finished')
        if finished:
            elapsed = time.time() - datetime.datetime.fromisoformat(finished.replace('Z', '+00:00')).timestamp()
            self.stop_event.wait(max(0, 6 * 3600 - elapsed))
        while not self.stop_event.is_set():
            try:
                self.catalog.scan()
            except Exception as error:
                print('catalog refresh failed:', type(error).__name__, flush=True)
            self.stop_event.wait(6 * 3600)

    def api(self, endpoint, params, repeated):
        light = ('ping', 'getLicense', 'getOpenSubsonicExtensions', 'getUser', 'getMusicFolders',
                 'getScanStatus', 'startScan', 'star', 'unstar', 'setRating', 'scrobble')
        scoped = endpoint in ('getSong', 'getLyrics', 'getLyricsBySongId') and params.get('id')
        if endpoint in light or scoped:
            return self._api(endpoint, params, repeated)
        with self.view_slots:
            return self._api(endpoint, params, repeated)

    def _api(self, endpoint, params, repeated):
        def amount(key, default=20):
            value = int(params.get(key, default))
            if not 0 <= value <= (100000 if key.endswith('Offset') or key == 'offset' else 500):
                raise APIError(10, 'Invalid pagination')
            return value

        def page(values, count='size', offset='offset', default=20):
            start = amount(offset, 0)
            return values[start:start + amount(count, default)]

        if endpoint in ('ping', 'getLicense', 'getOpenSubsonicExtensions'):
            return {'ping': {}, 'getLicense': {'license': {'valid': True, 'email': '', 'licenseExpires': '2099-01-01T00:00:00Z'}},
                    'getOpenSubsonicExtensions': {'openSubsonicExtensions': [{'name': 'songLyrics', 'versions': [1]}]}}[endpoint]
        if endpoint in ('getScanStatus', 'startScan'):
            if endpoint == 'startScan':
                self.catalog.start_scan()
            status = self.catalog.status()
            return {'scanStatus': {'scanning': bool(status.get('scan', {}).get('running')),
                                   'count': status['counts'].get('accepted', 0)}}
        if endpoint == 'getUser':
            username = self.account['username']
            if params.get('username', username) != username:
                raise APIError(50, 'Not authorized')
            return {'user': {'username': username, 'email': '', 'scrobblingEnabled': True,
                'adminRole': False, 'settingsRole': False, 'downloadRole': True, 'uploadRole': False,
                'playlistRole': False, 'coverArtRole': False, 'commentRole': False,
                'podcastRole': False, 'streamRole': True, 'jukeboxRole': False, 'shareRole': False,
                'videoConversionRole': False, 'folder': [int(k) for k in FOLDERS]}}
        if endpoint in ('star', 'unstar', 'setRating', 'scrobble'):
            ids = repeated.get('id', [])
            if not ids:
                raise APIError(10, 'Song id required')
            rating = int(params.get('rating', 0))
            if endpoint == 'setRating' and not 0 <= rating <= 5:
                raise APIError(10, 'Rating must be 0 to 5')
            if endpoint != 'scrobble' or params.get('submission', 'true') != 'false':
                self.catalog.annotate(ids, endpoint, rating)
            return {}
        if endpoint == 'getMusicFolders':
            return {'musicFolders': {'musicFolder': [{'id': int(k), 'name': NAMES[v]} for k, v in FOLDERS.items()]}}
        search_keys = None
        if endpoint in ('search2', 'search3'):
            query = params.get('query', '').strip().strip('"')
            query = '' if query == '*' else query
            if len(query) > 160:
                raise APIError(10, 'Search query is too long')
            # Validate before doing any network work, including zero-count scans.
            for kind in ('song', 'album', 'artist'):
                amount(kind + 'Count')
                amount(kind + 'Offset', 0)
            if query and any(amount(kind + 'Count') for kind in ('song', 'album', 'artist')):
                search_keys = self.search.search(query, params.get('musicFolderId'))
        scope = [params['id']] if endpoint in ('getSong', 'getLyrics', 'getLyricsBySongId') and params.get('id') else None
        library = Library(self.catalog, scope)
        public = library.public
        songs = library.filtered_songs(params.get('musicFolderId'))
        if endpoint in ('getArtists', 'getIndexes'):
            indexes = defaultdict(list)
            source = FOLDERS.get(params.get('musicFolderId'))
            for artist in sorted(library.artists.values(), key=lambda a: a['name']):
                if source and artist['_source'] != source:
                    continue
                first = artist['name'][:1].upper()
                indexes[first if first in string.ascii_uppercase else '#'].append(public(artist))
            node = {'ignoredArticles': '', 'index': [{'name': k, 'artist': v} for k, v in sorted(indexes.items())]}
            if endpoint == 'getIndexes':
                node['lastModified'] = int(time.time() * 1000)
            return {'artists' if endpoint == 'getArtists' else 'indexes': node}
        if endpoint in ('getAlbumList', 'getAlbumList2'):
            return {'albumList2' if endpoint.endswith('2') else 'albumList': {'album': [public(a) for a in page(library.album_list(params))]}}
        if endpoint == 'getSong':
            self.catalog.touch(params['id'])
            return {'song': library.songs[params['id']]}
        if endpoint == 'getArtist':
            artist = library.artists[params['id']]
            return {'artist': {**public(artist), 'album': [public(library.albums[k]) for k in sorted(artist['_albums'])]}}
        if endpoint == 'getAlbum':
            album = library.albums[params['id']]
            return {'album': {**public(album), 'song': [library.songs[k] for k in album['_songs']]}}
        if endpoint == 'getMusicDirectory':
            key = params['id']
            if key in library.albums:
                album = library.albums[key]
                return {'directory': {**public(album), 'child': [library.songs[k] for k in album['_songs']]}}
            if key in library.artists:
                artist = library.artists[key]
                return {'directory': {'id': key, 'name': artist['name'], 'child': [public(library.albums[k]) for k in sorted(artist['_albums'])]}}
            if key in FOLDERS:
                return {'directory': {'id': key, 'name': NAMES[FOLDERS[key]], 'child': [
                    {**public(a), 'title': a['name'], 'isDir': True} for a in library.artists.values() if a['_source'] == FOLDERS[key]]}}
            raise KeyError(key)
        if endpoint in ('search2', 'search3'):
            if search_keys is not None:
                found = [library.songs[k] for k in search_keys if k in library.songs]
            else:
                found = [s for s in songs if matches(s['title'] + ' ' + s['artist'] + ' ' + s['album'], query)]
                found.sort(key=lambda s: (s['title'], s['id']))
            source = FOLDERS.get(params.get('musicFolderId'))
            albums = [public(a) for a in library.albums.values() if (not source or a['_source'] == source) and matches(a['name'] + ' ' + a['artist'], query)]
            artists = [public(a) for a in library.artists.values() if (not source or a['_source'] == source) and matches(a['name'], query)]
            return {'searchResult3' if endpoint.endswith('3') else 'searchResult2': {
                'song': page(found, 'songCount', 'songOffset'), 'album': page(albums, 'albumCount', 'albumOffset'),
                'artist': page(artists, 'artistCount', 'artistOffset')}}
        if endpoint in ('getPlaylists', 'getPlaylist'):
            playlists = library.playlists(self.account['username'])
            if endpoint == 'getPlaylists':
                return {'playlists': {'playlist': [public(p) for p in playlists]}}
            playlist = next((p for p in playlists if p['id'] == params['id']), None)
            if playlist is None:
                raise KeyError(params['id'])
            return {'playlist': {**public(playlist), 'entry': playlist['_members']}}
        if endpoint == 'getRandomSongs':
            return {'randomSongs': {'song': self.discovery.select(library, amount('size', 10), params)}}
        if endpoint == 'getSongsByGenre':
            return {'songsByGenre': {'song': page([s for s in songs if s['genre'] == params.get('genre')], 'count', default=10)}}
        if endpoint == 'getGenres':
            return {'genres': {'genre': [{'value': name, 'songCount': sum(s['genre'] == name for s in songs),
                'albumCount': sum(a['genre'] == name for a in library.albums.values())} for name in NAMES.values()]}}
        if endpoint in ('getStarred', 'getStarred2'):
            return {'starred2' if endpoint.endswith('2') else 'starred': {'artist': [], 'album': [], 'song': [s for s in songs if s.get('starred')]}}
        if endpoint in ('getLyrics', 'getLyricsBySongId'):
            song = library.songs.get(params.get('id')) if params.get('id') else library.find_song(params.get('title', ''), params.get('artist', ''))
            text = self.metadata.get(song['id']).get('lrc', '') if song else ''
            artist, title = (song['artist'], song['title']) if song else (params.get('artist', ''), params.get('title', ''))
            if endpoint == 'getLyricsBySongId':
                return {'lyricsList': {'structuredLyrics': structured_lyrics(text, artist, title)}}
            return {'lyrics': {'artist': artist, 'title': title, 'value': text}}
        if endpoint in ('getArtistInfo', 'getArtistInfo2', 'getAlbumInfo', 'getAlbumInfo2'):
            if endpoint.startswith('getArtist'):
                related, seen = [], set()
                for song in library.similar_songs(params.get('id', '')):
                    key = song['artistId']
                    if key not in seen and key != params.get('id'):
                        related.append(public(library.artists[key]))
                        seen.add(key)
                return {endpoint[3:4].lower() + endpoint[4:]: {'biography': '', 'similarArtist': related[:amount('count', 20)]}}
            return {endpoint[3:4].lower() + endpoint[4:]: {'notes': ''}}
        if endpoint in ('getSimilarSongs', 'getSimilarSongs2'):
            return {'similarSongs2' if endpoint.endswith('2') else 'similarSongs': {
                'song': library.similar_songs(params.get('id', ''))[:amount('count', 50)]}}
        if endpoint == 'getTopSongs':
            return {'topSongs': {'song': library.top_songs(params.get('artist', ''), params.get('artistId'))[:amount('count', 50)]}}
        if endpoint in ('getNowPlaying', 'getBookmarks', 'getInternetRadioStations', 'getPodcasts'):
            return {endpoint[3:4].lower() + endpoint[4:]: {}}
        if endpoint == 'getPlayQueue':
            return {'playQueue': {'entry': []}}
        if endpoint == 'savePlayQueue':
            raise APIError(50, 'Server-side play queue is not implemented in this experiment')
        raise APIError(0, 'Endpoint not implemented in experiment: ' + endpoint)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Subsonic query strings contain credentials. Never log them.

    def log_request(self, code='-', size='-'):
        try:
            path = urlsplit(getattr(self, 'path', '')).path
        except ValueError:
            path = '<invalid-path>'
        if path == '/health':
            return
        print(json.dumps({'event': 'http', 'method': getattr(self, 'command', ''),
                          'path': path[:200], 'status': int(code),
                          'client': self.client_address[0]}, ensure_ascii=False), flush=True)

    def send_bytes(self, data, mime, status=200):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def reply(self, result, params, error=None):
        body = {'status': 'failed' if error else 'ok', 'version': '1.16.1',
                'type': 'musichub', 'serverVersion': '0.1.6', 'openSubsonic': True, **result}
        if error:
            body['error'] = {'code': error.code, 'message': error.message}
            print(json.dumps({'event': 'api_error', 'code': error.code,
                              'path': urlsplit(self.path).path[:200]}, ensure_ascii=False), flush=True)
        if params.get('f', 'xml') == 'json':
            self.send_bytes(json.dumps({'subsonic-response': body}, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
        else:
            self.send_bytes(xml_response(body), 'text/xml; charset=utf-8')

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        params = {}
        try:
            location = urlsplit(self.path)
            if location.path == '/health':
                return self.send_bytes(b'{"service":"music-hub","status":"ok"}', 'application/json')
            if location.path == '/':
                return self.send_bytes(json.dumps({'service': 'MusicHub',
                    'protocol': 'Subsonic 1.16.1', 'clientType': 'Subsonic', 'api': '/rest/',
                    'note': 'Multi-source music server. API authentication required.'}).encode(), 'application/json')
            if not location.path.startswith('/rest/'):
                return self.send_bytes(b'Not found', 'text/plain', 404)
            repeated = parse_qs(location.query, keep_blank_values=True, max_num_fields=2048)
            if self.command == 'POST':
                length = int(self.headers.get('Content-Length', 0))
                if not 0 <= length <= 65536:
                    return self.send_bytes(b'Request too large', 'text/plain', 413)
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                    return self.send_bytes(b'Expected form data', 'text/plain', 415)
                form = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True, max_num_fields=2048)
                repeated.update(form)
            params = {k: v[-1] for k, v in repeated.items()}
            if not authenticated(params, self.server.account):
                raise APIError(40, 'Wrong username or password')
            endpoint = location.path.removeprefix('/rest/').removesuffix('.view')
            if endpoint in ('stream', 'download'):
                return self.audio(params)
            if endpoint in ('getCoverArt', 'getAvatar'):
                key = params.get('id', params.get('username', ''))
                keys = self.server.catalog.cover_keys(key)
                cover_key = next((k for k in keys if (self.server.metadata.cached(k) or {}).get('cover_url')), None)
                actual = self.server.metadata.cover(cover_key) if cover_key else None
                if actual:
                    return self.send_bytes(*actual)
                # Unknown/stale client cover IDs should still resolve to an image,
                # never an XML error that a picture loader retries indefinitely.
                return self.send_bytes(cover_png(key[:200]), 'image/png')
            self.reply(self.server.api(endpoint, params, repeated), params)
        except APIError as error:
            self.reply({}, params, error)
        except KeyError:
            self.reply({}, params, APIError(70, 'Requested data not found or no longer playable'))
        except (ValueError, TypeError):
            self.reply({}, params, APIError(10, 'Invalid parameter or audio unavailable'))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            print('API error:', type(error).__name__, flush=True)
            self.reply({}, params, APIError(0, 'Temporarily unavailable; please retry'))

    def audio(self, params):
        key = params['id']
        if not re.fullmatch(r'[a-f0-9]{32}', key):
            raise KeyError(key)
        with self.server.media.open_audio(key) as (file, stream):
            size = os.fstat(stream.fileno()).st_size
            etag = '"' + str(os.fstat(stream.fileno()).st_mtime_ns) + '"'
            start, end = 0, size - 1
            requested = self.headers.get('Range')
            if self.headers.get('If-Range') not in (None, etag):
                requested = None
            if requested:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                if not match or not any(match.groups()):
                    return self.invalid_range(size)
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(end, int(right)) if right else end
                elif int(right) > 0:
                    start = max(0, size - int(right))
                else:
                    return self.invalid_range(size)
                if not 0 <= start <= end < size:
                    return self.invalid_range(size)
            mime = {'mp3': 'audio/mpeg', 'aac': 'audio/aac', 'm4a': 'audio/mp4', 'flac': 'audio/flac', 'ogg': 'audio/ogg', 'wav': 'audio/wav'}.get(file.suffix[1:], 'application/octet-stream')
            self.send_response(206 if requested else 200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'private, no-store')
            if requested:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            if urlsplit(self.path).path.endswith(('download', 'download.view')):
                record = self.server.catalog.song(key)
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(record['title'] + file.suffix))
            self.end_headers()
            if self.command == 'HEAD':
                return
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def invalid_range(self, size):
        self.send_response(416)
        self.send_header('Content-Range', f'bytes */{size}')
        self.send_header('Content-Length', '0')
        self.end_headers()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--data', default=str(Path(__file__).resolve().parent / 'data'))
    args = parser.parse_args()
    app = App((args.host, args.port), args.data)

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=app.refresh_loop, daemon=True, name='catalog-refresh').start()
    threading.Thread(target=app.discovery.loop, args=(app.stop_event,), daemon=True, name='music-discovery').start()
    threading.Thread(target=app.maintenance.loop, args=(app.stop_event,), daemon=True, name='disk-maintenance').start()
    threading.Thread(target=app.public_playlists.loop, args=(app.stop_event,), daemon=True, name='public-playlists').start()
    print(f'MusicHub listening on {args.port}; credentials stored in data/credentials.json', flush=True)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_event.set()
        app.server_close()


if __name__ == '__main__':
    main()
