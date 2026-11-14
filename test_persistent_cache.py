"""Persistent cache, disk-only API, daily batches, budgets and local/remote search."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

from catalog import Catalog
from library import Library
from media import CACHE_PLAYLIST, MediaCache, cache_day
from memory_budget import BackgroundPaused
from prefetch import Prefetch
from test_service import HTTPFixture, insert, record


class PersistentCacheTests(HTTPFixture):
    def cached(self, key, data=None, age=0):
        path = self.app.media.folder/(key+'.mp3')
        path.write_bytes(data if data is not None else self.audio+key.encode())
        os.utime(path,(time.time()-age,)*2)
        song = self.app.catalog.song(key)
        return self.app.media.register(key,song,path,180,hashlib.sha256(path.read_bytes()).hexdigest())

    def test_disk_playlist_json_xml_head_range_and_missing_never_calls_source(self):
        self.cached(self.ids[0])
        with patch('media.resolve',side_effect=AssertionError('network')):
            lists=self.api('getPlaylists')['playlists']['playlist']
            self.assertEqual(lists[0]['id'],CACHE_PLAYLIST)
            playlist=self.api('getPlaylist',{'id':CACHE_PLAYLIST})['playlist']
            self.assertEqual((playlist['songCount'],len(playlist['entry'])),(1,1))
            key=playlist['entry'][0]['id']
            self.assertTrue(key.startswith('cache-'))
            status,_,body=self.request('stream',{'id':key},{'Range':'bytes=0-2'})
            self.assertEqual((status,body),(206,b'ID3'))
            self.assertEqual(self.request('download',{'id':key},method='HEAD')[0],200)
            self.assertEqual(self.api('getSong',{'id':key})['song']['id'],key)
            self.api('star',{'id':key})
            _,_,xml=self.request('getPlaylist',{'id':CACHE_PLAYLIST,'f':'xml'})
            self.assertIn(key.encode(),xml)
            (self.app.media.folder/(self.ids[0]+'.mp3')).unlink()
            self.assertEqual(self.api('stream',{'id':key})['error']['code'],70)
            self.assertEqual(self.api('getPlaylist',{'id':CACHE_PLAYLIST})['playlist']['songCount'],0)

    def test_cross_source_metadata_and_file_hash_dedupe_versions_are_distinct(self):
        self.cached(self.ids[0])
        song=record('other');song.update(source='kuwo',title=record('1')['title'])
        alias=insert(self.app.catalog,song)
        with patch('media.resolve',side_effect=AssertionError('network')):
            self.assertEqual(self.app.media.prepare(alias).stem,self.ids[0])
        live=record('live');live.update(title=record('1')['title']+' (Live)')
        live_key=insert(self.app.catalog,live)
        self.cached(live_key)
        copy=insert(self.app.catalog,record('same-content'))
        self.cached(copy,data=(self.app.media.folder/(live_key+'.mp3')).read_bytes())
        self.assertEqual(self.app.media.summary()['count'],2)
        self.assertEqual(len(list(self.app.media.folder.glob('*.mp3'))),3) # 2 canonical + untouched fixture file

    def test_fifo_capacity_active_reader_daily_protection_and_cooldown(self):
        for path in self.app.media.folder.iterdir(): path.unlink()
        first=self.cached(self.ids[0],data=b'a'*80,age=40)
        second=self.cached(self.ids[1],data=b'b'*80,age=20)
        self.app.media.limit=100
        with self.app.media.open_audio('cache-'+self.ids[0]):
            self.app.media.cleanup()
            self.assertTrue(first.exists());self.assertFalse(second.exists())
        with self.app.catalog.connect() as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM cache_attempts WHERE song_id=? AND retry_at>?',(self.ids[1],time.time())).fetchone())
            db.execute('INSERT INTO daily_tracks VALUES (?,?)',(cache_day(),self.ids[0]))
        self.app.media.limit=50
        with self.assertRaises(BackgroundPaused): self.app.media.cleanup()
        self.assertTrue(first.exists())

    def test_persistence_retention_and_recovery_do_not_expire_valid_audio(self):
        self.cached(self.ids[0],age=40*86400)
        with self.app.catalog.connect() as db:
            db.execute('DELETE FROM boards');db.execute('UPDATE songs SET touched=?',(time.time()-40*86400,))
        self.app.maintenance.run()
        reopened=Catalog(self.state.name);media=MediaCache(reopened)
        with patch('media.resolve',side_effect=AssertionError('network')):
            self.assertTrue(media.prepare(self.ids[0],local_only=True).exists())
        with reopened.connect() as db: db.execute("UPDATE songs SET status='unavailable',checked=NULL WHERE id=?",(self.ids[0],))
        self.assertEqual(media.entry(self.ids[0])['id'],'cache-'+self.ids[0])
        self.assertEqual(self.api('getArtists')['status'],'ok')
        self.assertEqual(self.api('getAlbumList2',{'type':'newest'})['status'],'ok')

    def test_daily_fifty_stable_across_restart_and_cache_only_on_outage(self):
        for n in range(55):
            key=insert(self.app.catalog,record('daily-'+str(n)))
            self.cached(key)
        self.app.prefetch.fill_daily(cache_day())
        first=self.api('getRandomSongs',{'size':50})['randomSongs']['song']
        self.assertEqual(len(first),50)
        self.assertTrue(all(s['id'].startswith('cache-') for s in first))
        reopened=Prefetch(self.app.catalog,self.app.media,self.app.search)
        self.assertEqual([s['id'] for s in first],[s['id'] for s in reopened.daily(50,{})])
        with patch('prefetch.cache_day',return_value='2099-01-01'),patch.object(self.app.media,'prepare',side_effect=AssertionError('recommendations must not download')),patch.object(self.app.search,'search',side_effect=AssertionError('recommendations must not search')):
            next_day=reopened.daily(50,{})
            self.assertEqual(len(next_day),50)
            self.assertTrue(all(s['id'].startswith('cache-') for s in next_day))
            self.assertEqual([s['id'] for s in next_day],[s['id'] for s in reopened.daily(50,{})])
            self.assertEqual(self.app.media.summary()['count'],55)
        self.assertEqual(reopened.daily(0,{}),[])

    def test_background_pauses_for_memory_and_player_without_network(self):
        with patch('prefetch.background_allowed',return_value=False),patch.object(self.app.media,'prepare') as download:
            with self.assertRaises(BackgroundPaused): self.app.prefetch.step()
            download.assert_not_called()
        self.app.media.players=1
        with patch.object(self.app.media,'prepare') as download:
            with self.assertRaises(BackgroundPaused): self.app.prefetch.step()
            download.assert_not_called()
        self.app.media.players=0

    def test_fallback_to_matching_other_platform_and_retry_backoff(self):
        original=insert(self.app.catalog,record('fallback'))
        alternate_song=record('alternate');alternate_song.update(source='kuwo',title=record('fallback')['title'])
        alternate=insert(self.app.catalog,alternate_song);path=self.cached(alternate)
        prepare=self.app.media.prepare
        def download(key,**kwargs):
            if key==original: raise ValueError('source_down')
            return prepare(key,**kwargs)
        with patch.object(self.app.media,'prepare',side_effect=download),patch.object(self.app.search,'search',side_effect=AssertionError('unnecessary search')):
            self.assertEqual(self.app.prefetch.download(original),alternate)
        self.assertEqual(self.app.media.cached(original)[1],path)
        with self.app.catalog.connect() as db:
            self.assertGreater(db.execute('SELECT retry_at FROM cache_attempts WHERE song_id=?',(original,)).fetchone()[0],time.time())

    def test_local_remote_union_cached_first_and_query_cache_sees_new_local(self):
        local=record('local');local.update(title='合并搜索',singer='本地歌手')
        key=insert(self.app.catalog,local);self.cached(key)
        remote=record('remote');remote.update(source='kuwo',title='合并搜索',singer='线上歌手')
        duplicate={**local,'source':'qq','id':'copy'}
        media={'suffix':'mp3','size':1000,'duration':180,'duration_verified':True}
        with patch('online_search.source_search',side_effect=lambda source,q:[remote] if source=='kuwo' else [duplicate] if source=='qq' else []),patch('online_search.probe',return_value=media):
            result=self.api('search3',{'query':'合并搜索'})['searchResult3']['song']
            self.assertEqual(len(result),2);self.assertEqual(result[0]['id'],key)
            new={**local,'id':'later','singer':'后来歌手'}
            newkey=insert(self.app.catalog,new);self.cached(newkey)
            result=self.api('search3',{'query':'合并搜索'})['searchResult3']['song']
            self.assertEqual(len(result),3)
            self.assertIn(newkey,[r['id'] for r in result])

    def test_download_streaming_validation_and_partial_cleanup(self):
        key=insert(self.app.catalog,record('download'))
        response=MagicMock();response.headers={'Content-Length':'12'}
        response.iter_content.return_value=[b'ID3',b'abcdefghi'];response.__enter__.return_value=response
        info={'format':{'duration':180},'streams':[{'codec_type':'audio'}]}
        with patch('media.resolve',return_value=('https://music.126.net/audio',{},'mp3')),patch('media.requests.get',return_value=response),patch('media.inspect_audio',return_value=info):
            self.assertTrue(self.app.media.prepare(key).exists())
            response.iter_content.assert_called_once_with(65536)
        self.assertEqual(self.app.media.reserved,0)
        self.assertFalse((self.app.media.folder/(key+'.part')).exists())
        other=insert(self.app.catalog,record('interrupted'))
        response.iter_content.return_value=[b'ID3']
        with patch('media.resolve',return_value=('https://music.126.net/audio',{},'mp3')),patch('media.requests.get',return_value=response):
            with self.assertRaises(ValueError): self.app.media.prepare(other)
        self.assertFalse((self.app.media.folder/(other+'.part')).exists())
        self.assertIsNone(self.app.media.cached(other))

    def test_cache_and_playlist_endpoints_never_build_full_library(self):
        self.cached(self.ids[0]);self.app.prefetch.fill_daily(cache_day())
        with patch('service.Library',side_effect=AssertionError('eager library')):
            self.assertEqual(self.api('getPlaylist',{'id':CACHE_PLAYLIST})['status'],'ok')
            self.assertEqual(self.api('getPlaylists')['status'],'ok')
            self.assertEqual(self.api('getPlaylist',{'id':'netease-hot'})['status'],'ok')
            self.assertEqual(self.api('getRandomSongs',{'size':50})['status'],'ok')
            self.assertEqual(self.api('search3',{'query':'','songCount':1,'albumCount':0,'artistCount':0})['status'],'ok')

    def test_crash_recovery_adopts_complete_file_and_prunes_stale_partial(self):
        key=self.ids[0]
        (self.app.media.folder/(self.ids[1]+'.mp3')).write_bytes(self.audio+b'distinct')
        partial=self.app.media.folder/(format(999,'032x')+'.part')
        partial.write_bytes(b'incomplete');os.utime(partial,(time.time()-7200,)*2)
        info={'format':{'duration':180},'streams':[{'codec_type':'audio'}]}
        with patch('media.inspect_audio',return_value=info),patch('media.background_allowed',return_value=True):
            while self.app.media.reconcile_one():pass
        self.assertIsNotNone(self.app.media.cached(key,verified=True))
        self.assertFalse(partial.exists())
        self.assertEqual(self.api('getPlaylist',{'id':CACHE_PLAYLIST})['playlist']['songCount'],2)

    def test_single_download_slot_and_inflight_reservation_are_bounded(self):
        first=insert(self.app.catalog,record('parallel-a'));second=insert(self.app.catalog,record('parallel-b'))
        started,release=threading.Event(),threading.Event()
        response=MagicMock();response.headers={'Content-Length':'3'};response.__enter__.return_value=response
        def chunks(size):
            started.set();release.wait(5);yield b'ID3'
        response.iter_content.side_effect=chunks
        info={'format':{'duration':180},'streams':[{'codec_type':'audio'}]}
        with patch('media.resolve',return_value=('https://music.126.net/audio',{},'mp3')),patch('media.requests.get',return_value=response) as network,patch('media.inspect_audio',return_value=info):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(self.app.media.prepare,first)
                self.assertTrue(started.wait(2))
                self.assertGreater(self.app.media.reserved,0)
                try:
                    with self.assertRaises(BackgroundPaused):self.app.media.prepare(second,background=True)
                    self.assertEqual(network.call_count,1)
                finally:release.set()
                self.assertTrue(future.result(5).exists())
        self.assertEqual(self.app.media.reserved,0)

    def test_download_schedule_survives_worker_restart(self):
        day=cache_day()
        self.app.catalog.set_meta('prefetch',{'day':day,'hour':int(time.time()//3600),'attempts':1,'next_download':time.time()+180})
        worker=Prefetch(self.app.catalog,self.app.media,self.app.search);worker.next_reconcile=time.time()+60
        with patch('prefetch.background_allowed',return_value=True),patch.object(worker,'download') as download:
            worker.step();download.assert_not_called()

    def test_cache_playlist_is_first_even_empty_in_json_and_xml(self):
        self.assertEqual(self.api('getPlaylists')['playlists']['playlist'][0]['id'],CACHE_PLAYLIST)
        _,_,body=self.request('getPlaylists',{'f':'xml'})
        playlists=ET.fromstring(body).find('{*}playlists')
        self.assertEqual(playlists[0].attrib['id'],CACHE_PLAYLIST)

    def test_download_interval_defaults_to_one_minute_and_is_configurable(self):
        with patch.dict(os.environ):
            os.environ.pop('MUSICHUB_CACHE_INTERVAL', None)
            self.assertEqual(Prefetch(self.app.catalog,self.app.media,self.app.search).download_interval,60)
            os.environ['MUSICHUB_CACHE_INTERVAL']='180'
            worker=Prefetch(self.app.catalog,self.app.media,self.app.search)
            self.assertEqual(worker.download_interval,180)
            worker.next_reconcile=time.time()+600
            with patch('prefetch.background_allowed',return_value=True),patch.object(worker,'download',return_value=None) as download:
                before=time.time();worker.step();download.assert_called_once()
            self.assertGreaterEqual(self.app.catalog.status()['prefetch']['next_download'],before+180)
            os.environ['MUSICHUB_CACHE_INTERVAL']='5'
            self.assertEqual(Prefetch(self.app.catalog,self.app.media,self.app.search).download_interval,60)

    def test_continuous_one_minute_downloads_survive_restart_and_hour_boundary(self):
        for n in range(65):insert(self.app.catalog,record('paced-'+str(n)))
        worker=self.app.prefetch
        start=(int(time.time()//3600)+1)*3600
        worker.next_reconcile=start+7200
        clock=[start]
        def download(key):return self.cached(key).stem
        with patch('prefetch.time.time',side_effect=lambda:clock[0]),patch('prefetch.background_allowed',return_value=True),patch.object(worker,'download',side_effect=download) as network:
            for n in range(60):
                clock[0]=start+n*60
                worker.step()
                self.assertEqual(network.call_count,n+1)
                clock[0]+=1
                worker.step()  # No burst while waiting for the next download slot.
                self.assertEqual(network.call_count,n+1)
            self.assertEqual(self.app.media.summary()['count'],60)
            reopened=Prefetch(self.app.catalog,self.app.media,self.app.search)
            reopened.next_reconcile=start+7200
            with patch.object(reopened,'download',side_effect=download) as resumed:
                clock[0]=start+3599
                reopened.step();resumed.assert_not_called()
                clock[0]=start+3600
                reopened.step();resumed.assert_called_once()
            self.assertEqual(self.app.media.summary()['count'],61)

    def test_failures_and_duplicates_keep_one_minute_cadence_without_counting_success(self):
        worker=self.app.prefetch
        start=(int(time.time()//3600)+1)*3600
        clock=[start]
        self.cached(self.ids[0],age=100)
        worker.next_reconcile=start+7200
        def duplicate(key):
            self.app.media.bind(key,self.ids[0]);return self.ids[0]
        with patch('prefetch.time.time',side_effect=lambda:clock[0]),patch('prefetch.background_allowed',return_value=True):
            with patch.object(worker,'download',return_value=None) as failed:
                worker.step();failed.assert_called_once()
            clock[0]+=10
            with patch.object(worker,'download') as early:
                worker.step();early.assert_not_called()
            clock[0]=start+60
            with patch.object(worker,'download',side_effect=duplicate) as aliased:
                worker.step();aliased.assert_called_once()
            insert(self.app.catalog,record('new-after-duplicate'))
            clock[0]=start+120
            with patch.object(worker,'download',side_effect=lambda key:self.cached(key).stem) as new:
                worker.step();new.assert_called_once()
        with self.app.catalog.connect() as db:
            state=json.loads(db.execute("SELECT value FROM meta WHERE key='prefetch'").fetchone()[0])
        self.assertEqual(state['downloaded'],1)

    def test_download_worker_does_not_generate_or_depend_on_recommendations(self):
        self.app.prefetch.next_reconcile=time.time()+600
        with patch('prefetch.background_allowed',return_value=True),patch.object(self.app.prefetch,'fill_daily',side_effect=AssertionError('independent recommendation logic')),patch.object(self.app.prefetch,'download',return_value=None) as network:
            self.app.prefetch.step();network.assert_called_once()

    def test_public_playlist_priority_and_shared_file_skip_background_redownload(self):
        public = insert(self.app.catalog, record('public-first'), 'pending')
        other = insert(self.app.catalog, record('outside'))
        with self.app.catalog.connect() as db:
            for n in range(2):
                db.execute('INSERT INTO boards VALUES (?,?,?,?,?,?)',
                           ('pl-shared-'+str(n), 'netease', '共享歌单', 1, json.dumps([public]), '2026-11-13T00:00:00Z'))
        worker = self.app.prefetch
        worker.next_reconcile = time.time() + 3600
        with patch('prefetch.background_allowed', return_value=True), patch.object(worker, 'download', return_value=None) as download:
            worker.step()
            download.assert_called_once_with(public)
        # A manual playback uses the same cache index as background downloads.
        self.cached(public)
        self.app.catalog.set_meta('prefetch', {})
        with patch('prefetch.background_allowed', return_value=True), patch.object(worker, 'download', return_value=None) as download:
            worker.step()
            self.assertIn(download.call_args.args[0], self.ids)
            self.assertNotEqual(download.call_args.args[0], other)
        for n in range(2):
            entry = self.api('getPlaylist', {'id': 'pl-shared-'+str(n)})['playlist']['entry']
            self.assertEqual([s['id'] for s in entry], ['cache-'+public])

    def test_daytime_prefetch_continues_after_daily_and_cached_playlists(self):
        for key in self.ids:self.cached(key)
        for n in range(50):self.cached(insert(self.app.catalog,record('full-'+str(n))))
        self.app.prefetch.fill_daily(cache_day())
        new=insert(self.app.catalog,record('outside-cached-playlists'))
        self.app.prefetch.next_reconcile=time.time()+7200
        # 12:00 China time: the old night-only policy stopped here.
        noon=(int(time.time()//86400)*86400)+4*3600
        with patch('prefetch.time.time',return_value=noon),patch('prefetch.background_allowed',return_value=True),patch.object(self.app.prefetch,'download',return_value=None) as network:
            self.app.prefetch.step();network.assert_called_once_with(new)

    def test_stale_database_row_does_not_create_phantom_capacity(self):
        for path in self.app.media.folder.iterdir(): path.unlink()
        first=self.cached(self.ids[0],data=b'a'*100,age=40)
        second=self.cached(self.ids[1],data=b'b'*100,age=20)
        first.unlink()  # crash after unlink, before database cleanup
        self.app.media.limit=80
        self.app.media.cleanup()
        self.assertFalse(second.exists())
        self.assertEqual(self.app.media.summary()['bytes'],0)

    def test_rate_limited_source_is_persistently_paused_for_background_only(self):
        import requests
        response=MagicMock();response.status_code=429
        self.app.prefetch.retry(self.ids[0],requests.HTTPError(response=response))
        with self.app.catalog.connect() as db:
            sources=json.loads(db.execute("SELECT value FROM meta WHERE key='source_backoff'").fetchone()[0])
        self.assertGreater(sources['netease'],time.time()+1700)
        self.app.prefetch.next_reconcile=time.time()+600
        with patch('prefetch.background_allowed',return_value=True),patch.object(self.app.prefetch,'download') as download:
            self.app.prefetch.step();download.assert_not_called()

    def test_alias_cannot_point_to_evicted_file(self):
        with self.assertRaises(KeyError):self.app.media.bind(self.ids[0],'0'*32)
        with self.app.catalog.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM audio_aliases WHERE song_id=?',(self.ids[0],)).fetchone())

    def test_new_foreground_playback_interrupts_background_download(self):
        first=insert(self.app.catalog,record('background'));second=insert(self.app.catalog,record('foreground'))
        started,release=threading.Event(),threading.Event()
        response=MagicMock();response.headers={'Content-Length':'3'};response.__enter__.return_value=response
        def chunks(size):
            started.set();release.wait(3);yield b'ID3'
        response.iter_content.side_effect=chunks
        info={'format':{'duration':180},'streams':[{'codec_type':'audio'}]}
        with patch('media.background_allowed',return_value=True),patch('media.resolve',return_value=('https://music.126.net/audio',{},'mp3')),patch('media.requests.get',return_value=response),patch('media.inspect_audio',return_value=info):
            with ThreadPoolExecutor(max_workers=1) as pool:
                background=pool.submit(self.app.media.prepare,first,background=True)
                self.assertTrue(started.wait(2))
                timer=threading.Timer(.05,release.set);timer.start()
                with self.app.media.open_audio(second) as (path,stream):self.assertEqual(stream.read(),b'ID3')
                with self.assertRaises(BackgroundPaused):background.result(3)
                timer.join()
        self.assertIsNone(self.app.media.cached(first))
        self.assertEqual(self.app.media.reserved,0)
