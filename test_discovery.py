"""Recommendations expand beyond charts, avoid repeats and preserve playback IDs."""
from concurrent.futures import ThreadPoolExecutor
import json
import time
from unittest.mock import patch

from catalog import identifier, utc
from discovery import Discovery, identity, public_seeds
from library import Library
from test_service import HTTPFixture, insert, record


class DiscoveryTests(HTTPFixture):
    def recommend(self, endpoint, params):
        return {'randomSongs': {'song': self.app.discovery.select(Library(self.app.catalog), params.get('size',10), params)}}

    def populate(self, count=140, pool=90):
        keys = []
        for n in range(count):
            song = record('discovery-' + str(n))
            song.update(title='发现曲目' + str(n), singer='不同歌手' + str(n))
            key = insert(self.app.catalog, song)
            keys.append(key)
        with self.app.catalog.connect() as db:
            db.executemany('INSERT INTO discoveries VALUES (?,?)', ((k, time.time()) for k in keys[:pool]))
        return keys

    def test_two_fifty_song_batches_explore_and_do_not_repeat(self):
        keys = self.populate()
        first = self.recommend('getRandomSongs', {'size': 50})['randomSongs']['song']
        second = self.recommend('getRandomSongs', {'size': 50})['randomSongs']['song']
        self.assertEqual((len(first), len(second)), (50, 50))
        self.assertGreaterEqual(sum(s['id'] in keys[:90] for s in first), 40)
        self.assertFalse({identity(s) for s in first} & {identity(s) for s in second})
        self.assertEqual(len({identity(s) for s in first}), 50)
        self.assertEqual(self.api('getSong', {'id': first[0]['id']})['song']['id'], first[0]['id'])
        reopened = Discovery(self.app.catalog, self.app.search)
        third = reopened.select(Library(self.app.catalog), 30, {})
        self.assertFalse({identity(s) for s in first + second} & {identity(s) for s in third})

    def test_source_duplicates_played_tracks_and_artist_concentration(self):
        self.populate(90, 70)
        song = record('copy')
        song.update(source='kuwo', title='发现曲目0', singer='不同歌手0')
        copy = insert(self.app.catalog, song)
        original = identifier('netease', 'discovery-0')
        self.app.catalog.annotate([original], 'scrobble')
        songs = self.recommend('getRandomSongs', {'size': 50})['randomSongs']['song']
        self.assertNotIn(copy, [s['id'] for s in songs])
        self.assertNotIn(original, [s['id'] for s in songs])
        self.assertEqual(len({identity(s) for s in songs}), 50)
        for n in range(20):
            song = record('same-artist-' + str(n))
            song.update(title='同歌手曲目' + str(n), singer='同一个歌手')
            insert(self.app.catalog, song)
        songs = self.recommend('getRandomSongs', {'size': 30})['randomSongs']['song']
        self.assertLessEqual(sum(s['artist'] == '同一个歌手' for s in songs), 2)

    def test_concurrent_cards_are_disjoint(self):
        self.populate()
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(lambda _: self.app.discovery.select(Library(self.app.catalog), 50, {}), range(2)))
        self.assertFalse({identity(s) for s in batches[0]} & {identity(s) for s in batches[1]})

    def test_filters_zero_small_pool_and_excluded_tracks(self):
        keys = self.populate(3, 3)
        self.app.catalog.exclude(keys[0], 'trial')
        self.assertEqual(self.recommend('getRandomSongs', {'size': 0})['randomSongs']['song'], [])
        filtered = self.recommend('getRandomSongs', {'size': 50, 'musicFolderId': '2'})['randomSongs']['song']
        self.assertEqual(filtered, [])
        songs = self.recommend('getRandomSongs', {'size': 50, 'genre': '网易云'})['randomSongs']['song']
        self.assertNotIn(keys[0], [s['id'] for s in songs])
        self.assertTrue(all(s['genre'] == '网易云' for s in songs))
        self.assertEqual(len(songs), 4)
        self.assertTrue(self.app.discovery.wake.is_set())

    def test_refresh_only_adds_verified_off_chart_tracks_and_rotates_seeds(self):
        keys = self.populate(4, 0)
        with self.app.catalog.connect() as db:
            db.execute('UPDATE songs SET media=? WHERE id=?', (json.dumps({'duration_verified': True}), keys[0]))
        seeds = [{'id': str(n), 'query': '公开歌单种子' + str(n), 'category': '民谣'} for n in range(12)]
        with patch('discovery.public_seeds', return_value=[seeds[0]] * 3 + seeds), patch.object(self.app.search, 'search', return_value=keys + self.ids) as search:
            self.app.discovery.refresh()
            state = self.app.catalog.status()['discovery']
            self.assertEqual(state['seeds'], 6)
            self.assertEqual(state['new_tracks'], 1)
            self.assertFalse(state['running'])
            self.app.discovery.refresh()
            self.assertEqual(search.call_count, 12)
            self.assertEqual(len({c.args[0] for c in search.call_args_list}), 12)
        with self.app.catalog.connect() as db:
            self.assertEqual([r['id'] for r in db.execute('SELECT id FROM discoveries')], [keys[0]])

    def test_seed_failure_keeps_existing_pool_and_recommendations_work(self):
        keys = self.populate(4, 4)
        with patch('discovery.public_seeds', side_effect=TimeoutError):
            self.app.discovery.refresh()
        state = self.app.catalog.status()['discovery']
        self.assertEqual(state['error'], 'TimeoutError')
        self.assertFalse(state['running'])
        songs = self.recommend('getRandomSongs', {'size': 4})['randomSongs']['song']
        self.assertTrue(songs)

    def test_public_seed_fetches_varying_playlists_not_fixed_charts(self):
        from unittest.mock import MagicMock
        listing = MagicMock()
        listing.json.return_value = {'playlists': [{'id': 999}]}
        detail = MagicMock()
        detail.json.return_value = {'result': {'tracks': [{'id': i, 'name': '曲目' + str(i), 'artists': [{'name': '歌手'}]} for i in range(10)]}}
        with patch('discovery.requests.get', side_effect=[listing, detail] * 3) as fetch:
            seeds = public_seeds()
        self.assertEqual(len({s['category'] for s in seeds[:3]}), 3)
        self.assertTrue(all('歌手' in s['query'] for s in seeds))
        self.assertEqual(fetch.call_count, 6)
        self.assertTrue(all(c.kwargs['params']['id'] == 999 for c in fetch.call_args_list[1::2]))
