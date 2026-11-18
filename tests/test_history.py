"""History integration regressions against isolated space databases."""
import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from vivideo.app import app
from vivideo import storage

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, VIVIDEO_DATA_DIR=self.tmp.name)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.t = 1704038400000
        self.ids = []
        with storage.database('learning') as db:
            for vid, duration in [('a', 100), ('b', 120)]:
                db.execute('INSERT INTO videos VALUES (?,?,?,?)', (vid, 'https://example.com/'+vid, '', self.t))
                db.execute('INSERT INTO media_assets(video_id,title,duration) VALUES (?,?,?)', (vid, vid, duration))
            for vid, offset, watched, duration in [('a',60000,30,100),('a',3600000,60,100),('b',86460000,90,120)]:
                eid = str(uuid4()); self.ids.append(eid)
                db.execute('INSERT INTO watch_events VALUES (?,?,?,?,?,?,?)', (eid,vid,None,watched,duration,self.t+offset,self.t+offset))
        self.params = dict(from_ms=self.t, to_ms=self.t+3*86400000)

    def get(self, path, **params):
        return self.client.get('/api/spaces/learning/'+path, params={**self.params, **params})

    def test_statistics_and_daily_zeros(self):
        data = self.get('watch-stats').json()
        self.assertEqual([data[k] for k in ['event_count','video_count','watched_seconds','average_seconds']], [3,2,180,60])
        self.assertEqual([v['video_id'] for v in data['top_videos']], ['a','b'])
        days = self.get('watch-stats/daily').json()['items']
        self.assertEqual([d['date'] for d in days], ['2024-01-01','2024-01-02','2024-01-03'])
        self.assertEqual([d['watched_seconds'] for d in days], [90,90,0])

    def test_partial_day_and_empty_range(self):
        rows = self.get('watch-stats/daily', to_ms=self.t+120000).json()['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event_count'], 1)
        data = self.get('watch-stats', from_ms=self.t+2*86400000).json()
        self.assertEqual(data['average_seconds'], 0)

    def test_history_contract_filter_and_boundaries(self):
        data = self.get('watch-history', q='a', limit=1).json()
        self.assertEqual(data['total'], 2)
        self.assertIn('event_id', data['items'][0]); self.assertIn('url', data['items'][0])
        self.assertEqual(self.get('watch-history', from_ms=self.t+60000, to_ms=self.t+3600000).json()['total'], 1)

    def test_delete_and_isolation(self):
        url='/api/spaces/learning/watch-history/'+self.ids[0]
        self.assertEqual(self.client.delete(url.replace('/learning/','/roaming/')).status_code, 404)
        response=self.client.delete(url)
        self.assertEqual(response.status_code,204); self.assertEqual(response.content,b'')
        self.assertEqual(self.client.delete(url).status_code,404)
        self.assertEqual(self.get('watch-stats').json()['event_count'],2)

    def test_validation(self):
        for params in [dict(q='x'*101), dict(from_ms=-1),dict(to_ms=self.t),dict(to_ms=self.t+32*86400000),dict(from_ms=10**25,to_ms=10**25+100)]:
            self.assertEqual(self.get('watch-history', **params).status_code,422,params)
        self.assertEqual(self.client.get('/api/spaces/learning/watch-history',params={'from_ms':self.t}).status_code,422)
        self.assertEqual(self.client.get('/api/spaces/invalid/watch-history').status_code,422)
        self.assertEqual(self.client.delete('/api/spaces/learning/watch-history/no-uuid').status_code,422)

    def test_threshold_deduplication_and_cap(self):
        eid=str(uuid4())
        self.assertFalse(storage.record_watch('learning',eid,'a',None,25,100)['recorded'])
        self.assertTrue(storage.record_watch('learning',eid,'a',None,26,100)['recorded'])
        storage.record_watch('learning',eid,'a',None,150,100)
        with storage.database('learning') as db:
            db.execute('UPDATE watch_events SET created=? WHERE id=?',(self.t,eid))
        data=self.get('watch-history').json()
        self.assertEqual(data['total'],4)
        item=next(v for v in data['items'] if v['event_id']==eid)
        self.assertEqual(item['counted_seconds'],100); self.assertEqual(item['progress'],1)
