import os
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from vivideo.app import app
from vivideo import media,storage

REAL_ASYNC_CLIENT=httpx.AsyncClient
VIDEO_URL='https://www.bilibili.com/video/BV1B7411m7LV'


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'VIVIDEO_LOCAL_VIDEO':'0','VIVIDEO_DATA_DIR':self.directory.name})
        self.env.start()
        media.assets.clear();media.metadata_cache.clear();media.metadata_tasks.clear()
        self.client=TestClient(app)
        self.session=storage.create_session('learning')['id']
        with storage.database('learning') as db:
            db.execute('INSERT INTO videos VALUES (?,?,?,?)',('video_test',VIDEO_URL,'test',1))
            db.execute('INSERT INTO videos VALUES (?,?,?,?)',('video_second','https://www.youtube.com/watch?v=dQw4w9WgXcQ','test',1))
        self.calls=[]

    def tearDown(self):
        media.assets.clear();media.metadata_cache.clear();media.metadata_tasks.clear()
        self.env.stop();self.directory.cleanup()

    def upstream(self,request):
        self.calls.append(request)
        if request.url.path=='/x/web-interface/view':
            return httpx.Response(200,json={'code':0,'data':{'cid':123,'aid':456,'duration':100,'pic':'https://i0.hdslb.com/bfs/archive/poster.jpg'}})
        if request.url.path=='/x/player/playurl':
            return httpx.Response(200,json={'code':0,'data':{'timelength':100000,'durl':[{'url':'https://test.bilivideo.com/video.mp4?token=public'}]}})
        if request.url.host=='i0.hdslb.com':
            return httpx.Response(200,content=b'jpeg-fixture',headers={'content-type':'image/jpeg'})
        if request.url.host=='test.bilivideo.com':
            if request.headers.get('range')=='bytes=2-5':
                return httpx.Response(206,stream=httpx.ByteStream(b'cdef'),headers={'content-type':'video/mp4','content-length':'4','content-range':'bytes 2-5/10','accept-ranges':'bytes'})
            return httpx.Response(200,stream=httpx.ByteStream(b'abcdefghij'),headers={'content-type':'video/mp4','content-length':'10','accept-ranges':'bytes'})
        return httpx.Response(404)

    def transport(self,handler=None):
        return patch('vivideo.media.httpx.AsyncClient',side_effect=lambda **kwargs:REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler or self.upstream),**kwargs))

    def test_metadata_proxy_range_head_and_scope(self):
        with self.transport():
            info=self.client.get('/api/spaces/learning/videos/video_test/metadata')
            self.assertEqual(info.status_code,200,info.text)
            self.assertNotIn('_cid',info.json())
            image=self.client.get(info.json()['thumbnail_url'])
            self.assertEqual(image.content,b'jpeg-fixture')
            playback=self.client.get('/api/spaces/learning/videos/video_test/playback').json()
            self.assertEqual(playback['kind'],'native')
            result=self.client.get(playback['stream_url'],headers={'Range':'bytes=2-5'})
            self.assertEqual(result.status_code,206,result.text)
            self.assertEqual(result.content,b'cdef')
            self.assertEqual(result.headers['content-range'],'bytes 2-5/10')
            self.assertEqual(self.client.head(playback['stream_url']).status_code,200)
            self.assertEqual(self.client.get(playback['stream_url'],headers={'Range':'bytes=1-2,5-6'}).status_code,416)
            self.assertEqual(self.client.get('/api/spaces/roaming/videos/video_test/metadata').status_code,404)
            self.assertEqual(self.client.get('/api/spaces/learning/videos/video_second/playback').json()['kind'],'youtube')
            self.assertEqual(len([r for r in self.calls if r.url.path=='/x/web-interface/view']),1)

    def test_restricted_platform_and_preview_use_embed_without_tracking(self):
        with self.transport(lambda request:httpx.Response(412)):
            playback=self.client.get('/api/spaces/learning/videos/video_test/playback').json()
            self.assertEqual(playback['kind'],'bilibili')
            self.assertFalse(playback['tracking_supported'])
            self.assertIn('player.bilibili.com/player.html',playback['embed_url'])
        media.metadata_cache.clear()
        def clip(request):
            result=self.upstream(request)
            if request.url.path=='/x/player/playurl':
                return httpx.Response(200,json={'code':0,'data':{'timelength':10000,'durl':[{'url':'https://test.bilivideo.com/preview.mp4'}]}})
            return result
        with self.transport(clip):
            playback=self.client.get('/api/spaces/learning/videos/video_test/playback').json()
            self.assertEqual(playback['kind'],'bilibili')
            self.assertNotIn('stream_url',playback)

    def test_proxy_cannot_become_arbitrary_url_fetcher(self):
        for url in ['http://127.0.0.1/file.mp4','https://bilivideo.com.evil.example/file.mp4','https://user:secret@test.bilivideo.com/file.mp4']:
            self.assertIsNone(media.asset_url(url,'stream'))
        token=str(uuid4());media.assets[token]=media.Asset('https://test.bilivideo.com/video.mp4',time.monotonic()+60,'stream')
        def redirect(request):
            self.calls.append(request)
            return httpx.Response(302,headers={'location':'http://127.0.0.1/private'})
        with self.transport(redirect):
            self.assertEqual(self.client.get('/api/media/streams/'+token).status_code,502)
        self.assertEqual(len(self.calls),1)
        media.assets[token].expires=0
        self.assertEqual(self.client.get('/api/media/streams/'+token).status_code,404)

    def test_watch_threshold_dedup_and_space_isolation(self):
        payload={'event_id':str(uuid4()),'video_id':'video_test','session_id':self.session,'duration_seconds':100,'watched_seconds':24}
        path='/api/spaces/learning/watch-events'
        for watched in (24,25):
            payload['watched_seconds']=watched
            self.assertEqual(self.client.post(path,json=payload).json(),{'recorded':False})
        self.assertEqual(storage.list_watch_events('learning'),[])
        payload['watched_seconds']=25.001
        self.assertTrue(self.client.post(path,json=payload).json()['recorded'])
        payload['watched_seconds']=30;self.client.post(path,json=payload)
        payload['watched_seconds']=29;self.client.post(path,json=payload)
        events=storage.list_watch_events('learning')
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['watched_seconds'],30)
        self.assertEqual(storage.list_watch_events('roaming'),[])
        self.assertEqual(self.client.post('/api/spaces/roaming/watch-events',json=payload).status_code,404)
        payload['video_id']='video_second'
        self.assertEqual(self.client.post(path,json=payload).status_code,409)
        payload['event_id']=str(uuid4());payload['duration_seconds']=0
        self.assertEqual(self.client.post(path,json=payload).status_code,422)
        payload['duration_seconds']=100;payload['session_id']=str(uuid4())
        self.assertEqual(self.client.post(path,json=payload).status_code,404)
