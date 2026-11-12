"""Real model HTTP + MCP subprocess: clarification, atomic prefetch, cache selection."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from uuid import uuid4

from vivideo import agent, storage, library
from vivideo.model_configs import database as config_database

URL='https://www.youtube.com/watch?v=dQw4w9WgXcQ'


class Model(BaseHTTPRequestHandler):
    mode='normal'
    def log_message(self,*args):pass
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if data['tools'][0]['function']['name']=='select_ready_videos':
            candidate=json.loads(data['messages'][-1]['content'])['candidates'][0]
            message={'role':'assistant','content':None,'tool_calls':[{'id':uuid4().hex,'type':'function','function':{'name':'select_ready_videos','arguments':json.dumps({'recommendations':[{'video_id':candidate['id'],'reason':'符合 Python 实践需求'}]})}}]}
            out=json.dumps({'choices':[{'message':message}]}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(out)));self.end_headers();self.wfile.write(out);return
        current=max(i for i,m in enumerate(data['messages']) if m['role']=='user')
        results=[m for m in data['messages'][current+1:] if m['role']=='tool']
        if self.mode=='fail' and results:
            self.send_response(401);self.end_headers();return
        names=[t['function']['name'] for t in data['tools']]
        assert 'prepare_domain_videos' in names and 'query_local_videos' in names
        first='首次' in data['messages'][current]['content']
        if not results:
            calls=[('prepare_domain_videos',{'topic':'机器学习','queries':['机器学习 Python']})]
            if not first:calls.append(('query_local_videos',{'query':'机器学习','ready_only':False}))
        else:
            items=[]
            if not first:
                for r in results:
                    payload=json.loads(r['content'])
                    for v in payload.get('videos',[]):items.append({'video_id':v['id'],'reason':'符合 Python 实践方向'})
            calls=[('respond_to_user',{'text':'你希望学习原理还是 Python 实践？' if first else '可以从这段 Python 实践开始。','recommendations':items})]
        message={'role':'assistant','content':None,'tool_calls':[{'id':uuid4().hex,'type':'function','function':{'name':n,'arguments':json.dumps(a)}} for n,a in calls]}
        out=json.dumps({'choices':[{'message':message}]}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(out)));self.end_headers();self.wfile.write(out)


class LocalAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'VIVIDEO_DATA_DIR':self.directory.name,'VIVIDEO_LOCAL_VIDEO':'1'});self.env.start()
        self.http=ThreadingHTTPServer(('127.0.0.1',0),Model);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start();Model.mode='normal'
        with config_database() as db:
            db.execute('INSERT INTO model_configs VALUES (?,?,?,?,?)',('model','fixture',f'http://127.0.0.1:{self.http.server_port}/v1','model','secret'))
            db.execute('INSERT INTO model_selections VALUES (?,?)',('learning','model'))
        self.sid=storage.create_session('learning')['id']

    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join();self.env.stop();self.directory.cleanup()

    def turn(self,text):return asyncio.run(agent.process_turn('learning',self.sid,uuid4().hex,text))

    def test_clarify_then_cache_recommendation_through_mcp(self):
        first=self.turn('首次想了解机器学习')
        self.assertEqual(first['videos'],[]);self.assertEqual(first['preparation']['phase'],'clarifying')
        self.assertEqual(len(library.cache_status('learning')['jobs']),1)
        vid=library.import_video('learning',URL)['video_id'];path=library.root('learning')/'assets'/vid;path.mkdir(parents=True);(path/'video.mp4').write_bytes(b'fixture')
        with storage.database('learning') as db:
            db.execute("UPDATE media_assets SET state='ready',title='机器学习 Python',relative_path=?,duration=10,bytes=7 WHERE video_id=?",(f'assets/{vid}/video.mp4',vid))
        second=self.turn('Python 动手实践，十分钟左右')
        self.assertEqual(second['preparation']['phase'],'recommending');self.assertEqual(second['videos'][0]['id'],vid);self.assertTrue(second['videos'][0]['local'])
        self.assertNotIn('secret',json.dumps(storage.get_session('learning',self.sid)))

    def test_late_candidate_auto_selection_without_extra_user_turn(self):
        self.turn('首次想了解机器学习');second=self.turn('Python 动手实践')
        self.assertEqual(second['videos'],[])
        vid=library.import_video('learning',URL)['video_id'];path=library.root('learning')/'assets'/vid;path.mkdir(parents=True);(path/'video.mp4').write_bytes(b'fixture')
        with storage.database('learning') as db:
            db.execute("UPDATE media_assets SET state='ready',title='机器学习 Python',relative_path=?,duration=10,bytes=7 WHERE video_id=?",(f'assets/{vid}/video.mp4',vid))
            plan=library.latest_plan(db,self.sid)
            db.execute('INSERT INTO plan_candidates VALUES (?,?)',(plan['id'],vid))
            library.schedule_selection(db,plan['id'])
        asyncio.run(agent.select_cached_videos('learning',plan['id']))
        data=library.session_preparation('learning',self.sid)
        self.assertEqual(data['turns'][-1]['videos'][0]['id'],vid)
        self.assertEqual(len(storage.get_session('learning',self.sid)['history']),4)
        # No repeated background model calls once an approved selection exists.
        with patch('vivideo.agent.call_model',side_effect=AssertionError('must not call twice')):
            asyncio.run(agent.select_cached_videos('learning',plan['id']))

    def test_late_selector_cannot_publish_after_topic_changes(self):
        self.turn('首次机器学习');self.turn('Python 实践')
        vid=library.import_video('learning',URL)['video_id'];path=library.root('learning')/'assets'/vid;path.mkdir(parents=True);(path/'video.mp4').write_bytes(b'fixture')
        with storage.database('learning') as db:
            db.execute("UPDATE media_assets SET state='ready',title='机器学习 Python',relative_path=?,duration=10,bytes=7 WHERE video_id=?",(f'assets/{vid}/video.mp4',vid))
            plan=library.latest_plan(db,self.sid);db.execute('INSERT INTO plan_candidates VALUES (?,?)',(plan['id'],vid))
        async def slow_response(*args):
            rid=str(uuid4());storage.begin_request('learning',self.sid,rid,'改看威尼斯',True)
            storage.complete_request('learning',self.sid,rid,'改看威尼斯',{'message':'更喜欢城市还是水巷？','videos':[]},'memory',[],[],preparation={'topic':'威尼斯','queries':['威尼斯 漫步'],'new_topic':True})
            return {'tool_calls':[{'function':{'name':'select_ready_videos','arguments':json.dumps({'recommendations':[{'video_id':vid,'reason':'旧主题视频'}]})}}]}
        with patch('vivideo.agent.call_model',side_effect=slow_response):asyncio.run(agent.select_cached_videos('learning',plan['id']))
        data=library.session_preparation('learning',self.sid)
        self.assertEqual(data['preparation']['topic'],'威尼斯')
        self.assertTrue(all(not t['videos'] for t in data['turns']))

    def test_model_failure_does_not_commit_staged_plan(self):
        Model.mode='fail'
        with self.assertRaises(agent.AgentError):self.turn('首次学习机器学习')
        self.assertEqual(library.cache_status('learning')['jobs'],[])
        self.assertEqual(storage.get_session('learning',self.sid)['history'],[])
