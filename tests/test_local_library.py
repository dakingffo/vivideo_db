"""Durable local media lifecycle, revision isolation, quotas, cancellation and playback."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from vivideo.app import app
from vivideo import storage, library, download_worker

URL='https://www.youtube.com/watch?v=dQw4w9WgXcQ'
URL2='https://www.bilibili.com/video/BV1B7411m7LV'


class LocalLibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'VIVIDEO_DATA_DIR':self.directory.name,'VIVIDEO_LOCAL_VIDEO':'1','VIVIDEO_VIDEO_MAX_BYTES':'1024','VIVIDEO_MEDIA_QUOTA_BYTES':'100000'})
        self.env.start();self.client=TestClient(app);self.sid=storage.create_session('learning')['id']

    def tearDown(self):
        self.env.stop();self.directory.cleanup()

    def complete(self,message='想了解机器学习',draft=None,items=None):
        rid=str(uuid4());storage.begin_request('learning',self.sid,rid,message,True)
        result=storage.complete_request('learning',self.sid,rid,message,{'message':'好的，已收到。','videos':items or []},'# memory',
            [{'id':v['id'],'url':v['url'],'title':v.get('title','测试视频')} for v in items or []],[],preparation=draft)
        return result

    def ready(self,url=URL,body=b'0123456789',title='机器学习 Python 入门',pinned=False):
        vid=library.import_video('learning',url,pinned)['video_id']
        directory=library.root('learning')/'assets'/vid;directory.mkdir(parents=True,exist_ok=True)
        (directory/'video.mp4').write_bytes(body);(directory/'cover.webp').write_bytes(b'cover')
        with storage.database('learning') as db:
            db.execute("UPDATE media_assets SET state='ready',title=?,relative_path=?,thumbnail_path=?,duration=10,bytes=?,last_access=? WHERE video_id=?",(title,f'assets/{vid}/video.mp4',f'assets/{vid}/cover.webp',len(body)+5,time.time(),vid))
            db.execute("UPDATE download_jobs SET status='complete',reserved=0 WHERE target=?",(vid,))
        return vid

    def test_import_dedup_scope_and_no_arbitrary_urls(self):
        first=library.import_video('learning',URL)
        self.assertEqual(library.import_video('learning',URL+'&tracking=1'),first)
        self.assertEqual(len(library.cache_status('learning')['jobs']),1)
        self.assertEqual(library.query_videos('learning'),[])
        self.assertEqual(library.cache_status('roaming')['assets'],[])
        for url in ('http://127.0.0.1/private','https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ','file:///etc/passwd'):
            self.assertEqual(self.client.post('/api/spaces/learning/local-videos',json={'url':url}).status_code,422)

    def test_first_clarification_and_next_ready_selection(self):
        vid=self.ready();video=library.query_videos('learning')[0]
        first=self.complete(draft={'topic':'机器学习','queries':['机器学习 入门']},items=[video])
        self.assertEqual(first['videos'],[])
        self.assertEqual(first['preparation']['phase'],'clarifying');self.assertIn('？',first['message'])
        second=self.complete('想动手训练 Python 模型',{'topic':'机器学习','queries':['机器学习 Python 入门']},[video])
        self.assertEqual(second['preparation']['phase'],'recommending')
        self.assertEqual([v['id'] for v in second['videos']],[vid]);self.assertTrue(second['videos'][0]['local'])
        self.assertEqual(len(storage.get_session('learning',self.sid)['history']),4)

    def test_pending_publish_only_model_approved_and_current_revision(self):
        self.complete(draft={'topic':'机器学习','queries':['Python 教程']})
        pending=library.import_video('learning',URL)['video_id']
        other=library.import_video('learning',URL2)['video_id']
        item=library.query_videos('learning','',False)[0]
        second=self.complete('Python 动手实践',{'topic':'机器学习','queries':['Python']},[item])
        self.assertEqual(second['videos'],[])
        selected=item['id'];selected_url=item['url'];self.ready(selected_url)
        state=library.session_preparation('learning',self.sid)
        self.assertEqual([v['id'] for v in state['turns'][-1]['videos']],[selected])
        self.assertNotIn(other if selected==pending else pending,[v['id'] for v in state['turns'][-1]['videos']])
        # A new topic is clarified and never receives the previous revision's picks.
        third=self.complete('改看威尼斯',{'topic':'威尼斯','queries':['威尼斯 漫步'],'new_topic':True})
        self.assertEqual(third['preparation']['phase'],'clarifying')
        self.assertEqual(library.session_preparation('learning',self.sid)['turns'][-1]['videos'],[])

    def test_transaction_rollback_delete_and_cancel_prevent_late_work(self):
        rid=str(uuid4());storage.begin_request('learning',self.sid,rid,'test',True);storage.cancel_request('learning',rid)
        with self.assertRaises(storage.StateError):
            storage.complete_request('learning',self.sid,rid,'test',{'message':'明确吗？','videos':[]},'memory',[],[],preparation={'topic':'t','queries':['t']})
        self.assertEqual(library.cache_status('learning')['jobs'],[])
        self.complete(draft={'topic':'t','queries':['t']});storage.delete_session('learning',self.sid)
        self.assertTrue(all(j['status']=='cancelled' for j in library.cache_status('learning')['jobs']))
        with self.assertRaises(storage.StateError):library.session_preparation('learning',self.sid)

    def test_leases_recover_and_old_owner_cannot_finish(self):
        jid=library.import_video('learning',URL)['job_id'];job=library.claim_job('learning','owner1')
        self.assertEqual(job['id'],jid);self.assertIsNone(library.claim_job('learning','owner2'))
        with storage.database('learning') as db:db.execute('UPDATE download_jobs SET lease_until=0 WHERE id=?',(jid,))
        recovered=library.claim_job('learning','owner2');self.assertEqual(recovered['attempts'],2)
        self.assertFalse(library.heartbeat('learning',jid,'owner1'))
        library.finish_job('learning',job)
        self.assertEqual(library.cache_status('learning')['jobs'][0]['status'],'running')
        library.finish_job('learning',recovered,'failed')
        with storage.database('learning') as db:db.execute('UPDATE download_jobs SET not_before=0 WHERE id=?',(jid,))
        last=library.claim_job('learning','owner3');library.finish_job('learning',last,'failed')
        self.assertEqual(library.cache_status('learning')['jobs'][0]['status'],'failed')
        self.assertEqual(library.cache_status('learning')['reserved_bytes'],0)

    def test_quota_pins_playback_and_failed_reservation_is_nondestructive(self):
        vid=self.ready(body=b'x'*500,pinned=True)
        library.import_video('learning',URL2)
        with storage.database('learning') as db:db.execute('INSERT INTO media_settings VALUES(1,?)',(3200,))
        self.assertIsNone(library.claim_job('learning','owner'))
        self.assertTrue(library.asset_path('learning',f'assets/{vid}/video.mp4').exists())
        self.client.patch('/api/spaces/learning/local-videos/'+vid,json={'pinned':False})
        library.ready_asset('learning',vid,touch=True)
        self.assertIsNone(library.claim_job('learning','owner'))
        self.assertEqual(self.client.delete('/api/spaces/learning/local-videos/'+vid).status_code,409)
        with storage.database('learning') as db:
            db.execute('UPDATE media_assets SET playback_until=0 WHERE video_id=?',(vid,));db.execute('UPDATE download_jobs SET not_before=0')
        self.assertIsNotNone(library.claim_job('learning','owner'))
        self.assertFalse((library.root('learning')/f'assets/{vid}/video.mp4').exists())
        self.assertNotIn(vid,[a['video_id'] for a in library.cache_status('learning')['assets']])

    def test_local_range_head_metadata_and_path_traversal(self):
        vid=self.ready();base='/api/spaces/learning'
        self.assertEqual(self.client.get(base+'/videos/'+vid+'/playback').json()['provider'],'local')
        self.assertEqual(self.client.get(base+'/videos/'+vid+'/metadata').json()['duration_seconds'],10)
        url=base+'/local-videos/'+vid+'/stream'
        result=self.client.get(url,headers={'Range':'bytes=2-5'});self.assertEqual(result.status_code,206);self.assertEqual(result.content,b'2345')
        self.assertEqual(self.client.head(url).headers['content-length'],'10')
        self.assertEqual(self.client.get(url.replace('learning','roaming')).status_code,409)
        self.assertEqual(self.client.get(url.replace('/stream','/unknown')).status_code,404)
        with storage.database('learning') as db:db.execute("UPDATE media_assets SET relative_path='../../../../etc/passwd' WHERE video_id=?",(vid,))
        self.assertEqual(self.client.get(url).status_code,404)

    def test_search_prefetch_is_bounded_and_reuses_cache(self):
        self.complete(draft={'topic':'机器学习','queries':['机器学习']})
        rows=[{'url':URL,'title':'机器学习 Python 入门'},{'url':URL2,'title':'入门视频'}]
        job=library.claim_job('learning','searcher')
        with patch('vivideo.download_worker.search_videos',return_value=rows):asyncio.run(download_worker.run_job('learning',job))
        status=library.cache_status('learning')
        self.assertEqual(len(status['assets']),2)
        self.assertEqual(len([j for j in status['jobs'] if j['kind']=='download']),2)
        self.assertEqual(library.plan_context('learning',self.sid)['status'],'prepared')
        self.assertEqual(library.cache_status('roaming')['jobs'],[])

    def test_cleanup_removes_row_from_cache_and_candidate_lists(self):
        vid=self.ready()
        self.complete(draft={'topic':'机器学习','queries':['机器学习']})
        with storage.database('learning') as db:
            plan=library.latest_plan(db,self.sid)
            db.execute('INSERT INTO plan_candidates VALUES (?,?)',(plan['id'],vid))
        result=self.client.delete('/api/spaces/learning/local-videos/'+vid)
        self.assertEqual(result.status_code,204)
        self.assertEqual(library.cache_status('learning')['assets'],[])
        self.assertEqual(library.query_videos('learning',ready_only=False),[])
        self.assertEqual(library.plan_context('learning',self.sid)['candidates'],[])
        self.assertFalse((library.root('learning')/f'assets/{vid}').exists())
        library.reconcile('learning')
        self.assertEqual(library.cache_status('learning')['assets'],[])

    def test_reconciliation_and_retry_missing_files(self):
        vid=self.ready();(library.root('learning')/f'assets/{vid}/video.mp4').unlink()
        library.reconcile('learning');self.assertNotIn(vid,[a['video_id'] for a in library.cache_status('learning')['assets']])
        self.assertIsNotNone(library.import_video('learning',URL)['job_id'])

    def test_cancelled_subprocess_is_terminated_and_does_not_publish(self):
        import sys
        job=library.claim_job('learning','empty')
        self.assertIsNone(job)
        library.import_video('learning',URL);job=library.claim_job('learning','owner')
        staging=library.root('learning')/'staging'/job['id']/job['owner'];staging.mkdir(parents=True)
        pidfile=staging/'pid'
        args=[sys.executable,'-c','import os,sys,time;open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(30)',str(pidfile)]
        start=time.monotonic()
        with patch('vivideo.library.heartbeat',return_value=False):
            with self.assertRaises(download_worker.DownloadError):asyncio.run(download_worker.command(args,'learning',job,staging))
        self.assertLess(time.monotonic()-start,5)
        with self.assertRaises(ProcessLookupError):os.kill(int(pidfile.read_text()),0)
        self.assertEqual(library.query_videos('learning'),[])

    def test_watch_uses_authoritative_local_duration(self):
        vid=self.ready();base='/api/spaces/learning/watch-events'
        payload={'event_id':str(uuid4()),'video_id':vid,'session_id':self.sid,'watched_seconds':.3,'duration_seconds':1}
        self.assertEqual(self.client.post(base,json=payload).status_code,422)
        payload.update(duration_seconds=10,watched_seconds=2.5)
        self.assertFalse(self.client.post(base,json=payload).json()['recorded'])
        payload['watched_seconds']=2.501
        self.assertTrue(self.client.post(base,json=payload).json()['recorded'])

    def test_idle_chat_does_not_prefetch(self):
        self.complete('你好');self.assertEqual(library.cache_status('learning')['jobs'],[])
