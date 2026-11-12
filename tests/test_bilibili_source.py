"""Bilibili API-first resolution, per-part durations and safe error classification."""
import asyncio
import copy
import unittest
from unittest.mock import patch

import httpx
from vivideo import bilibili_source,download_worker

REAL_CLIENT=httpx.AsyncClient
URL='https://www.bilibili.com/video/BV1B7411m7LV'


class BilibiliSourceTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.info={'aid':123,'bvid':'BV1B7411m7LV','cid':100,'title':'课程','desc':'简介','duration':9000,
                   'owner':{'name':'作者'},'pages':[{'cid':100,'duration':10,'part':'第一节'},{'cid':200,'duration':8990,'part':'后续'}]}
        self.play={'timelength':10000,'quality':32,'dash':{
            'video':[{'id':32,'codecid':7,'baseUrl':'https://test.bilivideo.com/video.m4s?token=private-cdn-token','codecs':'avc1.64001e','width':852,'height':480,'bandwidth':400000}],
            'audio':[{'id':30280,'baseUrl':'https://test.bilivideo.com/audio.m4s','codecs':'mp4a.40.2','bandwidth':64000}]}}
    def transport(self,handler=None):
        return patch('vivideo.bilibili_source.httpx.AsyncClient',side_effect=lambda **kw:REAL_CLIENT(transport=httpx.MockTransport(handler or self.upstream),**kw))
    def upstream(self,request):
        self.calls.append(request)
        if request.url.host=='www.bilibili.com':return httpx.Response(200,headers={'set-cookie':'buvid3=anonymous-fixture; Domain=.bilibili.com; Path=/'})
        if request.url.path.endswith('/view'):
            self.assertEqual(request.url.params.get('bvid'),'BV1B7411m7LV')
            self.assertIn('anonymous-fixture',request.headers.get('cookie',''))
            return httpx.Response(200,json={'code':0,'data':self.info})
        if request.url.path.endswith('/playurl'):
            self.assertEqual(request.url.params['avid'],'123');self.assertEqual(request.url.params['cid'],'100')
            return httpx.Response(200,json={'code':0,'data':self.play})
        return httpx.Response(404)
    def test_resolves_api_without_video_page_and_uses_first_part(self):
        with self.transport():data=asyncio.run(bilibili_source.resolve(URL))
        self.assertEqual(data['duration'],10);self.assertIn('P1 第一节',data['title'])
        self.assertEqual(len(data['formats']),2);self.assertEqual(data['formats'][0]['height'],480)
        self.assertTrue(all(f['http_headers']['Range']=='bytes=0-' for f in data['formats']))
        self.assertEqual(data['formats'][1]['vcodec'],'none')
        self.assertFalse(any('/video/' in r.url.path for r in self.calls))
        self.assertTrue(all(r.headers.get('origin')=='https://www.bilibili.com' for r in self.calls))
    def test_unsupported_primary_cdn_uses_valid_backup(self):
        self.play['dash']['audio'][0]['backupUrl']=['https://test.bilivideo.com/audio.m4s']
        self.play['dash']['audio'][0]['baseUrl']='https://upos-hz-mirrorakam.akamaized.net/audio.m4s'
        with self.transport():data=asyncio.run(bilibili_source.resolve(URL))
        audio=next(f for f in data['formats'] if f['vcodec']=='none')
        self.assertEqual(audio['url'],'https://test.bilivideo.com/audio.m4s')

    def test_stops_on_412_without_blind_endpoint_retries(self):
        with self.transport(lambda r:httpx.Response(412)):
            with self.assertRaises(bilibili_source.SourceError) as e:asyncio.run(bilibili_source.resolve(URL))
        self.assertFalse(e.exception.retryable);self.assertIn('412',str(e.exception))
    def test_preview_or_untrusted_cdn_never_becomes_download_info(self):
        for change in [{'is_preview':True},{'timelength':1000}]:
            with self.subTest(change=change):
                original=self.play;self.play={**copy.deepcopy(original),**change}
                with self.transport(),self.assertRaises(bilibili_source.SourceError):asyncio.run(bilibili_source.resolve(URL))
                self.play=original
        self.play['dash']['video'][0]['baseUrl']='http://127.0.0.1/private'
        with self.transport(),self.assertRaises(bilibili_source.SourceError):asyncio.run(bilibili_source.resolve(URL))
    def test_cookie_or_api_errors_are_sanitized_and_terminal(self):
        def handler(request):
            if request.url.host=='www.bilibili.com':return httpx.Response(200)
            return httpx.Response(200,json={'code':-101,'message':'private-cdn-token'})
        with self.transport(handler):
            with self.assertRaises(bilibili_source.SourceError) as e:asyncio.run(bilibili_source.resolve(URL))
        self.assertFalse(e.exception.retryable);self.assertNotIn('private-cdn-token',str(e.exception))
    def test_progressive_fallback_still_requires_full_length(self):
        self.play={'timelength':10000}
        original=self.upstream
        def handler(request):
            if request.url.params.get('fnval')=='1':
                self.calls.append(request)
                return httpx.Response(200,json={'code':0,'data':{'timelength':10000,'quality':16,'durl':[{'url':'https://test.bilivideo.com/full.mp4','size':12000}]}})
            return original(request)
        with self.transport(handler):data=asyncio.run(bilibili_source.resolve(URL))
        self.assertEqual(data['formats'][0]['height'],360);self.assertEqual(len(self.calls),4)
    def test_redirects_are_bounded_and_cannot_target_internal_hosts(self):
        original=self.upstream
        def good(request):
            if str(request.url)=='https://www.bilibili.com/':return httpx.Response(302,headers={'location':'https://www.bilibili.com/?entry=home'})
            return original(request)
        with self.transport(good):self.assertTrue(asyncio.run(bilibili_source.resolve(URL))['formats'])
        for target in ('http://127.0.0.1/private','https://www.bilibili.com.evil.test/private','https://user:secret@www.bilibili.com/private'):
            with self.transport(lambda r:httpx.Response(302,headers={'location':target})):
                with self.assertRaises(bilibili_source.SourceError):asyncio.run(bilibili_source.resolve(URL))

    def test_command_error_does_not_disclose_provider_output(self):
        error=download_worker.process_error(b'ERROR HTTP Error 412: Precondition Failed https://secret/?token=private-token Cookie: secret','python')
        self.assertFalse(error.retryable);self.assertIn('412',str(error));self.assertNotIn('private-token',str(error));self.assertNotIn('Cookie',str(error))
        self.assertFalse(download_worker.process_error(b'Requested format is not available','python').retryable)
        self.assertIn('567',str(download_worker.process_error(b'Got error: HTTP Error 567','python')))
