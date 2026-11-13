"""Translate verified chart tracks into ordinary Subsonic library objects."""
import json
import random
import re
import unicodedata

from catalog import identifier
from resolver import NAMES

FOLDERS = {str(i): source for i, source in enumerate(NAMES, 1)}


def matches(text, query):
    return all(word in text.casefold() for word in query.casefold().split())


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value)).casefold() if c.isalnum())


def artist_names(value):
    return {normalized(name) for name in re.split(r'[、,，;&；]|\s+feat\.?\s+', value, flags=re.I) if normalized(name)}


def song_entry(row):
    record, media = row['record'], row['media']
    source = record['source']
    artist = record.get('singer') or '未知歌手'
    album = record.get('album') or NAMES[source] + ' · 榜单精选'
    artist_id = 'ar' + identifier(source, artist)
    album_id = 'al' + identifier(source, artist + '\0' + album)
    suffix = media.get('suffix', 'mp3')
    song = {'id': row['key'], 'parent': album_id, 'isDir': False,
            'title': record['title'], 'album': album, 'artist': artist,
            'artistId': artist_id, 'albumId': album_id,
            'coverArt': album_id, 'duration': int(media.get('duration') or 0),
            'size': int(media.get('size') or 0), 'suffix': suffix,
            'contentType': {'aac': 'audio/aac', 'm4a': 'audio/mp4', 'mp3': 'audio/mpeg', 'flac': 'audio/flac', 'ogg': 'audio/ogg', 'wav': 'audio/wav', 'ape': 'audio/ape'}.get(suffix, 'audio/mpeg'),
            'bitRate': int(media.get('bitrate') or 128),
            'genre': NAMES[source], 'type': 'music', 'isVideo': False,
            'path': f'{NAMES[source]}/{artist}/{album}/{record["title"]}.{suffix}',
            'created': row['checked'] or '1970-01-01T00:00:00Z', 'playCount': row['play_count'] or 0,
            'userRating': row['rating'] or 0}
    if row['starred']:
        song['starred'] = row['starred']
    return song


class Library:
    def __init__(self, catalog, keys=None):
        rows, boards = catalog.iter_visible(keys), catalog.boards()
        self.songs, self.albums, self.artists = {}, {}, {}
        self.boards = boards
        for row in rows:
            song = song_entry(row)
            source = row['record']['source']
            artist, album = song['artist'], song['album']
            artist_id, album_id = song['artistId'], song['albumId']
            self.songs[song['id']] = song
            self.albums.setdefault(album_id, {'id': album_id, 'parent': artist_id, 'isDir': True,
                'name': album, 'title': album, 'artist': artist, 'artistId': artist_id,
                'coverArt': album_id, 'songCount': 0, 'duration': 0, 'genre': NAMES[source],
                'created': song['created'], 'playCount': 0, '_source': source, '_songs': []})
            entry = self.albums[album_id]
            entry['_songs'].append(song['id'])
            entry['songCount'] += 1
            entry['duration'] += song['duration']
            entry['playCount'] += song['playCount']
            entry['created'] = max(entry['created'], song['created'])
            self.artists.setdefault(artist_id, {'id': artist_id, 'name': artist, 'coverArt': artist_id,
                                              'albumCount': 0, '_albums': set(), '_source': source})
            self.artists[artist_id]['_albums'].add(album_id)
        for artist in self.artists.values():
            artist['albumCount'] = len(artist['_albums'])
        self.popularity = {key: 0.0 for key in self.songs}
        self.memberships = {key: set() for key in self.songs}
        for board in boards:
            weight = 3 if board['id'].endswith('-hot') else 1
            for rank, key in enumerate(json.loads(board['members']), 1):
                if key in self.songs:
                    self.popularity[key] += weight / rank
                    self.memberships[key].add(board['id'])

    @staticmethod
    def public(value):
        return {key: item for key, item in value.items() if not key.startswith('_')}

    def filtered_songs(self, folder=None):
        source = FOLDERS.get(str(folder)) if folder else None
        return [s for s in self.songs.values() if not source or s['genre'] == NAMES[source]]

    def find_song(self, title, artist=''):
        names = artist_names(artist)
        found = [s for s in self.songs.values() if normalized(s['title']) == normalized(title)
                 and (not names or names & artist_names(s['artist']))]
        # Prefer a source with a direct lyric ID when a legacy request has no ID.
        found.sort(key=lambda s: (s['genre'] != '网易云', -self.popularity[s['id']]))
        return found[0] if found else None

    def seed_songs(self, key):
        if key in self.songs:
            return [self.songs[key]]
        if key in self.albums:
            return [self.songs[k] for k in self.albums[key]['_songs']]
        if key in self.artists:
            return [s for s in self.songs.values() if s['artistId'] == key]
        return []

    @staticmethod
    def unique_tracks(songs):
        result, seen = [], set()
        for song in songs:
            identity = (normalized(song['title']), normalized(song['artist']))
            if identity not in seen:
                result.append(song)
                seen.add(identity)
        return result

    def top_songs(self, artist='', artist_id=None):
        names = artist_names(artist)
        if artist_id in self.artists:
            names |= artist_names(self.artists[artist_id]['name'])
        values = [s for s in self.songs.values() if not names or names & artist_names(s['artist'])]
        values.sort(key=lambda s: (-self.popularity[s['id']], -s['playCount'], s['id']))
        return self.unique_tracks(values)

    def similar_songs(self, key):
        seeds = self.seed_songs(key)
        if not seeds:
            return []
        identities = {(normalized(s['title']), normalized(s['artist'])) for s in seeds}
        names = set().union(*(artist_names(s['artist']) for s in seeds))
        albums = {s['albumId'] for s in seeds}
        boards = set().union(*(self.memberships[s['id']] for s in seeds))
        def score(song):
            return (100 * len(names & artist_names(song['artist'])) +
                    20 * (song['albumId'] in albums) + 5 * len(boards & self.memberships[song['id']]))
        values = [s for s in self.songs.values()
                  if (normalized(s['title']), normalized(s['artist'])) not in identities and score(s) > 0]
        values.sort(key=lambda s: (-score(s), -self.popularity[s['id']], s['id']))
        return self.unique_tracks(values)

    def album_list(self, params):
        folder = FOLDERS.get(params.get('musicFolderId'))
        values = [a for a in self.albums.values() if not folder or a['_source'] == folder]
        kind = params.get('type', 'alphabeticalByName')
        if kind == 'random':
            random.shuffle(values)
        elif kind == 'starred':
            values = [a for a in values if any('starred' in self.songs[s] for s in a['_songs'])]
        elif kind == 'byGenre':
            values = [a for a in values if a['genre'] == params.get('genre')]
        elif kind == 'byYear':
            values = []  # Providers do not consistently publish album years.
        elif kind in ('newest', 'recent', 'frequent'):
            values.sort(key=lambda a: a['created'] if kind == 'newest' else a['playCount'], reverse=True)
        else:
            values.sort(key=lambda a: (a['artist'] if kind == 'alphabeticalByArtist' else a['name'], a['id']))
        return values

    def playlists(self, username):
        output = []
        for board in self.boards:
            members = [self.songs[k] for k in json.loads(board['members']) if k in self.songs]
            is_public_playlist = board['id'].startswith('pl-')
            if is_public_playlist and not members:
                continue
            output.append({'id': board['id'], 'name': board['name'], 'owner': username,
                           'public': False, 'songCount': len(members),
                           'duration': sum(s['duration'] for s in members),
                           'created': board['updated'], 'changed': board['updated'],
                           'comment': (f'公开歌单；过滤会员/试听并探测音频；原单 {board["total"]} 首；最多读取300首，匿名接口可能只返回部分曲目'
                                       if is_public_playlist else f'仅收录公开可播完整音频；原榜 {board["total"]} 首'),
                           'coverArt': members[0]['coverArt'] if members else None, '_members': members})
        return output
