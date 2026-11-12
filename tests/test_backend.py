"""Real gateway -> ZMQ worker -> MCP subprocess -> local HTTP model/search fixture."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from vivideo.app import app
from vivideo import storage
from vivideo.search import canonical_video_url

ROOT=Path(__file__).resolve().parents[1]
VIDEO='https://www.bilibili.com/video/BV1xx411c7mD'


class FixtureHandler(BaseHTTPRequestHandler):
    calls=[]
    slow_started=threading.Event()
    slow_release=threading.Event()

    def log_message(self,*args):
        pass

    def reply(self,status,data):
        encoded=json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.reply(200,{'results':[
            {'url':VIDEO+'?tracking=1','title':'Agent 实战','content':'Python 教程'},
            {'url':VIDEO,'title':'重复结果'},
            {'url':'https://example.com/article','title':'不是视频'},
            {'url':'javascript:alert(1)','title':'不安全地址'}]})

    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.calls.append(data)
        user_index=max(index for index,m in enumerate(data['messages']) if m['role']=='user')
        user=data['messages'][user_index]['content']
        tool_messages=[m for m in data['messages'][user_index+1:] if m['role']=='tool']
        if '慢请求' in user and not tool_messages:
            self.slow_started.set()
            self.slow_release.wait(15)
        if '认证失败' in user or ('中途失败' in user and len(tool_messages)>1):
            self.reply(401,{'error':'secret-provider-error-must-not-leak'})
            return
        if any('空回复' in m['content'] for m in data['messages'] if m['role']=='user'):
            message={'role':'assistant','content':'   '}
        elif '伪造推荐' in user:
            message=self.tools([('respond_to_user',{'text':'不应通过','recommendations':[{'video_id':'invented-id','reason':'模型猜测'}]})])
        elif '只聊聊' in user:
            message={'role':'assistant','content':'当然，你今天想聊什么？'}
        elif not tool_messages:
            message=self.tools([('search_web_videos',{'query':'AI Agent Python 实战'})])
        elif len(tool_messages)==1:
            rows=json.loads(tool_messages[0]['content'])['videos']
            ids=[v['id'] for v in rows]
            evidence='我喜欢代码示例' if '我喜欢代码示例' in user else '不存在的用户原话'
            message=self.tools([
                ('save_video_links',{'video_ids':ids}),
                ('update_session_memory',{'markdown':'# 会话记忆\n\n偏好代码示例，想学习 Agent。'}),
                ('propose_space_traits',{'traits':[{'text':'偏好包含代码示例的讲解','evidence':evidence}]})])
        else:
            rows=json.loads(tool_messages[0]['content'])['videos']
            message=self.tools([('respond_to_user',{'text':'这条包含代码示例，可以从这里开始。',
                'recommendations':[{'video_id':v['id'],'reason':'符合你的实践需求'} for v in rows]})])
        self.reply(200,{'choices':[{'message':message}]})

    @staticmethod
    def tools(calls):
        return {'role':'assistant','content':None,'reasoning_content':'fixture reasoning context','tool_calls':[
            {'id':str(uuid4()),'type':'function','function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}} for name,args in calls]}


class BackendIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http=ThreadingHTTPServer(('127.0.0.1',0),FixtureHandler)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0))
            port=probe.getsockname()[1]
        endpoint=f'tcp://127.0.0.1:{port}'
        self.env=patch.dict(os.environ,{'VIVIDEO_LOCAL_VIDEO':'0','VIVIDEO_DATA_DIR':self.directory.name,'VIVIDEO_SEARCH_PROVIDER':'searxng',
            'SEARXNG_URL':f'http://127.0.0.1:{self.http.server_port}','VIVIDEO_ZMQ_ENDPOINT':endpoint,
            'PYTHONPATH':str(ROOT/'python')})
        self.env.start()
        self.mode=patch('vivideo.app.MODE','zmq');self.mode.start()
        self.endpoint=patch('vivideo.app.ENDPOINT',endpoint);self.endpoint.start()
        self.log=open(Path(self.directory.name)/'worker.log','w+')
        self.worker=subprocess.Popen([sys.executable,'-m','vivideo.worker'],cwd=ROOT,env=dict(os.environ),stdout=self.log,stderr=self.log)
        self.client=TestClient(app)
        for _ in range(10):
            if self.client.get('/api/health').json()['backend_connected']:
                break
            if self.worker.poll() is not None:
                self.log.seek(0)
                self.fail(self.log.read())
            time.sleep(.1)
        else:
            self.fail('Worker did not become ready')
        self.config=self.client.post('/api/model-configs',json={'name':'Fixture','base_url':f'http://127.0.0.1:{self.http.server_port}/v1',
            'model':'fixture-model','api_key':'private-auth-key'}).json()
        for space in storage.SPACES:
            self.client.put('/api/model-configs/selection/'+space,json={'config_id':self.config['id']})
        FixtureHandler.calls=[]
        FixtureHandler.slow_started.clear();FixtureHandler.slow_release.clear()

    def tearDown(self):
        FixtureHandler.slow_release.set()
        self.worker.terminate()
        try:
            self.worker.wait(8)
        except subprocess.TimeoutExpired:
            self.worker.kill();self.worker.wait()
        self.log.close()
        self.endpoint.stop();self.mode.stop();self.env.stop();self.directory.cleanup()

    def session(self,space='learning'):
        result=self.client.post(f'/api/spaces/{space}/sessions',json={})
        self.assertEqual(result.status_code,201,result.text)
        return result.json()['id']

    def chat(self,sid,message='我喜欢代码示例，请推荐 Agent 视频',**changes):
        payload={'session_id':sid,'request_id':str(uuid4()),'message':message,'space_id':'learning'}
        payload.update(changes)
        return self.client.post('/api/chat',json=payload)

    def test_full_turn_confirmation_persistence_and_isolation(self):
        sid=self.session();rid=str(uuid4())
        response=self.chat(sid,request_id=rid)
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertTrue(result['message']);self.assertFalse(result['demo'])
        self.assertEqual([v['url'] for v in result['videos']],[VIDEO])
        self.assertEqual(len(result['trait_proposals']),1)
        self.assertEqual(self.client.get('/api/spaces/learning/traits').json()['traits'],[])
        snapshot=self.client.get('/api/spaces/learning/sessions/'+sid).json()
        self.assertEqual(len(snapshot['history']),2)
        memory_path=Path(self.directory.name)/'spaces/learning/sessions'/sid/'memory.md'
        self.assertEqual(memory_path.read_text(),snapshot['memory'])
        self.assertEqual(self.client.get('/api/spaces/roaming/sessions').json()['sessions'],[])
        self.assertEqual(self.client.get('/api/spaces/roaming/sessions/'+sid).status_code,404)
        self.assertEqual(self.client.get('/api/spaces/roaming/videos').json()['videos'],[])
        self.assertEqual(len(self.client.get('/api/spaces/learning/videos').json()['videos']),1)
        call_count=len(FixtureHandler.calls)
        self.assertEqual(self.chat(sid,request_id=rid).json(),result)
        self.assertEqual(len(FixtureHandler.calls),call_count)
        self.assertEqual(self.chat(sid,'不同内容',request_id=rid).status_code,409)
        pid=result['trait_proposals'][0]['id']
        decision=self.client.post('/api/spaces/learning/trait-proposals/'+pid+'/decision',json={'accept':True,'text':'喜欢代码实战'})
        self.assertEqual(decision.status_code,200)
        self.assertEqual(decision.json()['traits'][0]['text'],'喜欢代码实战')
        self.assertEqual(self.client.post('/api/spaces/learning/trait-proposals/'+pid+'/decision',json={'accept':True}).status_code,409)
        roam_sid=self.session('roaming')
        roam_result=self.chat(roam_sid,space_id='roaming').json()
        roam_pid=roam_result['trait_proposals'][0]['id']
        ignored=self.client.post('/api/spaces/roaming/trait-proposals/'+roam_pid+'/decision',json={'accept':False})
        self.assertEqual(ignored.json()['traits'],[])
        self.assertEqual(storage.get_session('roaming',roam_sid)['trait_proposals'][0]['status'],'rejected')
        self.assertIn('喜欢代码实战',(Path(self.directory.name)/'spaces/learning/memory.md').read_text())
        # Personalized context contains only confirmed traits; API secrets never enter model messages.
        self.assertEqual(self.chat(sid,'只聊聊',personalized=False).status_code,200)
        prior_assistants=[m for m in FixtureHandler.calls[-1]['messages'] if m['role']=='assistant']
        self.assertTrue(any(m.get('reasoning_content')=='fixture reasoning context' for m in prior_assistants))
        self.assertNotIn('_protocol_turn',json.dumps(self.client.get('/api/spaces/learning/sessions/'+sid).json()))
        self.assertNotIn('喜欢代码实战',FixtureHandler.calls[-1]['messages'][1]['content'])
        self.assertEqual(self.chat(sid,'只聊聊',personalized=True).status_code,200)
        self.assertIn('喜欢代码实战',FixtureHandler.calls[-1]['messages'][1]['content'])
        self.assertNotIn('private-auth-key',json.dumps(FixtureHandler.calls))
        self.client.put('/api/model-configs/'+self.config['id'],json={'name':'Fixture edited','base_url':self.config['base_url'],'model':'different-model'})
        self.assertEqual(self.chat(sid,'只聊聊').status_code,200)
        self.assertFalse(any(m['role']=='assistant' for m in FixtureHandler.calls[-1]['messages']))
        self.assertEqual(self.client.delete('/api/spaces/learning/sessions/'+sid).status_code,204)
        self.assertFalse(memory_path.exists())
        self.assertEqual(self.chat(sid).status_code,404)
        self.assertEqual(len(self.client.get('/api/spaces/learning/traits').json()['traits']),1)

    def test_failure_rollback_no_recommendations_and_missing_config(self):
        sid=self.session()
        failed=self.chat(sid,'中途失败')
        self.assertEqual(failed.status_code,502,failed.text)
        self.assertNotIn('secret-provider-error',failed.text)
        self.assertEqual(storage.get_session('learning',sid)['history'],[])
        self.assertEqual(storage.list_videos('learning'),[])
        self.assertEqual(storage.get_session('learning',sid)['memory'],'')
        plain=self.chat(sid,'只聊聊')
        self.assertEqual(plain.status_code,200,plain.text)
        self.assertEqual(plain.json()['videos'],[])
        self.assertTrue(storage.get_session('learning',sid)['memory'])
        self.client.put('/api/model-configs/selection/learning',json={'config_id':None})
        self.assertEqual(self.chat(sid).status_code,409)

    def test_cancel_delete_during_turn_and_invalid_evidence(self):
        sid=self.session();rid=str(uuid4())
        result={}
        thread=threading.Thread(target=lambda:result.update(response=self.chat(sid,'慢请求',request_id=rid)))
        thread.start()
        self.assertTrue(FixtureHandler.slow_started.wait(10))
        self.assertEqual(self.chat(sid,'慢请求',request_id=rid).status_code,409)
        self.assertEqual(self.chat(sid,'不同的并发请求').status_code,409)
        self.assertEqual(self.client.delete('/api/spaces/learning/sessions/'+sid).status_code,204)
        FixtureHandler.slow_release.set();thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertIn(result['response'].status_code,(404,409))
        self.assertEqual(storage.list_videos('learning'),[])
        sid=self.session();rid=str(uuid4())
        self.client.post('/api/spaces/learning/requests/'+rid+'/cancel')
        self.assertEqual(self.chat(sid,request_id=rid).status_code,409)
        response=self.chat(sid,'推荐视频')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['trait_proposals'],[])

    def test_empty_output_and_unknown_video_ids_are_rejected(self):
        for message in ('空回复','伪造推荐'):
            sid=self.session()
            response=self.chat(sid,message)
            self.assertEqual(response.status_code,502,response.text)
            self.assertEqual(storage.get_session('learning',sid)['history'],[])
            self.assertEqual(storage.list_videos('learning'),[])


class CanonicalLinkTests(unittest.TestCase):
    def test_direct_urls_only(self):
        self.assertEqual(canonical_video_url(VIDEO+'/?tracking=1'),VIDEO)
        self.assertEqual(canonical_video_url('https://youtu.be/dQw4w9WgXcQ?t=10'),'https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        for url in ['javascript:alert(1)','https://bilibili.com.evil.test/video/BV123','https://www.bilibili.com/search','https://youtube.com/watch','https://user:pass@vimeo.com/123']:
            self.assertIsNone(canonical_video_url(url))
