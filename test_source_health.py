"""Automatic-download source circuit breaker; manual search/playback remain independent."""
import datetime
import hashlib
import json
import os
import time
from unittest.mock import patch

import requests

from memory_budget import BackgroundPaused
from prefetch import Prefetch
from resolver import NAMES
from test_service import HTTPFixture, insert, record


class SourceHealthTests(HTTPFixture):
    def health(self):
        with self.app.catalog.connect() as db:
            row=db.execute("SELECT value FROM meta WHERE key='download_source_health'").fetchone()
        return json.loads(row['value']) if row else {}

    def song(self, source):
        return insert(self.app.catalog,{**record('policy-'+source),'source':source})

    def test_three_failures_disable_only_that_source_until_next_china_day(self):
        now=datetime.datetime(2026,11,13,23,0,tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()
        key=self.song('kugou')
        with patch('prefetch.time.time',return_value=now):
            self.app.prefetch.retry(key,requests.ConnectionError())
            self.app.prefetch.retry(key,requests.ConnectionError())
            self.assertIn('kugou',self.app.prefetch.available_sources())
            self.app.prefetch.retry(key,requests.ConnectionError())
            worker=Prefetch(self.app.catalog,self.app.media,self.app.search)
            self.assertNotIn('kugou',worker.available_sources())
            self.assertIn('kuwo',worker.available_sources())
            self.assertEqual(self.health()['kugou']['blocked_until'],now+3600)
        with patch('prefetch.time.time',return_value=now+3601):
            self.assertIn('kugou',worker.available_sources())
            worker.retry(key,requests.ConnectionError())
            self.assertEqual(self.health()['kugou']['failures'],1)
            self.assertIn('kugou',worker.available_sources())

    def test_success_resets_own_streak_and_failures_do_not_cross_sources(self):
        key=self.song('kugou')
        for _ in range(2):self.app.prefetch.retry(key,ValueError('unavailable'))
        self.app.prefetch.source_result('kuwo')
        self.assertEqual(self.health()['kugou']['failures'],2)
        self.app.prefetch.source_result('kugou')
        self.app.prefetch.retry(key,ValueError('unavailable'))
        self.assertEqual(self.health()['kugou']['failures'],1)
        self.assertIn('kugou',self.app.prefetch.available_sources())

    def test_every_source_disabled_pauses_downloads_and_recovers_next_day(self):
        now=time.time()
        with patch('prefetch.time.time',return_value=now):
            for source in NAMES:
                key=self.song(source)
                for _ in range(3):self.app.prefetch.retry(key,requests.ConnectionError())
            worker=Prefetch(self.app.catalog,self.app.media,self.app.search);worker.next_reconcile=now+2*86400
            self.assertEqual(worker.available_sources(),[])
            with patch('prefetch.background_allowed',return_value=True),patch.object(worker,'download') as network,patch.object(self.app.search,'search') as search:
                worker.step();network.assert_not_called();search.assert_not_called()
        with patch('prefetch.time.time',return_value=now+86400),patch('prefetch.background_allowed',return_value=True),patch.object(worker,'download',return_value=None) as network:
            worker.step();network.assert_called_once()

    def test_background_fallback_search_skips_disabled_source_but_manual_search_does_not(self):
        key=self.song('kugou')
        for _ in range(3):self.app.prefetch.retry(key,ValueError('unavailable'))
        with patch('online_search.source_search',return_value=[]) as request:
            self.app.search.search('source policy check',sources=self.app.prefetch.available_sources())
            self.assertNotIn('kugou',[c.args[0] for c in request.call_args_list])
            request.reset_mock()
            self.app.search.search('source policy check')
            self.assertEqual({c.args[0] for c in request.call_args_list},set(NAMES))

    def test_matching_alternate_and_primary_cannot_download_from_disabled_source(self):
        original=self.ids[0]
        alternate=insert(self.app.catalog,{**record('1'),'source':'kugou'})
        for _ in range(3):self.app.prefetch.retry(alternate,requests.ConnectionError())
        with patch.object(self.app.media,'prepare',side_effect=ValueError('source down')) as download,patch.object(self.app.search,'search',return_value=[alternate]) as search:
            self.assertIsNone(self.app.prefetch.download(original))
            self.assertEqual([c.args[0] for c in download.call_args_list],[original])
            self.assertNotIn('kugou',search.call_args.kwargs['sources'])
            with self.assertRaises(BackgroundPaused):self.app.prefetch.download(alternate)
            self.assertEqual(download.call_count,1)

    def test_local_resource_pauses_do_not_mark_source_as_failed(self):
        for error in (BackgroundPaused('memory'),ValueError('磁盘剩余空间不足，暂停新增缓存'),PermissionError()):
            with patch.object(self.app.media,'prepare',side_effect=error),patch.object(self.app.search,'search') as search:
                with self.assertRaises(BackgroundPaused):self.app.prefetch.download(self.ids[0])
                search.assert_not_called()
        self.assertEqual(self.health(),{})

    def test_actual_new_cache_resets_streak_but_old_cache_hit_does_not(self):
        key=self.ids[0];worker=self.app.prefetch
        for _ in range(2):worker.retry(key,ValueError('source down'))
        def cached(*args,**kwargs):
            path=self.app.media.folder/(key+'.mp3');path.write_bytes(self.audio)
            return self.app.media.register(key,self.app.catalog.song(key,allow_unavailable=True),path,180,hashlib.sha256(self.audio).hexdigest())
        with patch.object(self.app.media,'prepare',side_effect=cached):
            worker.fetch(key,self.app.catalog.song(key,allow_unavailable=True))
        self.assertEqual(self.health()['netease']['failures'],0)
        worker.retry(key,ValueError('source down'))
        path=self.app.media.folder/(key+'.mp3');os.utime(path,(time.time()-100,)*2)
        with self.app.catalog.connect() as db:db.execute('UPDATE audio_files SET added=? WHERE id=?',(time.time()-100,key))
        worker.fetch(key,self.app.catalog.song(key))
        self.assertEqual(self.health()['netease']['failures'],1)
