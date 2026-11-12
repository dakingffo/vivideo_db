import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from vivideo.app import app
from vivideo import service_status

REAL_CLIENT = httpx.AsyncClient


class ServiceStatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'VIVIDEO_DATA_DIR': self.directory.name})
        self.env.start()
        service_status._cache.clear()
        self.client = TestClient(app)

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def configure(self):
        config = self.client.post('/api/model-configs', json={
            'name': '测试', 'base_url': 'https://provider.example/v1',
            'model': 'test-model', 'api_key': 'private-secret',
        }).json()
        self.client.put('/api/model-configs/selection/learning', json={'config_id': config['id']})
        return config

    def test_unconfigured_and_database_failure(self):
        data = self.client.get('/api/service-status').json()
        self.assertEqual(data, {'database': {'state': 'connected'}, 'model_api': {'state': 'unconfigured'}})
        with patch.object(service_status.storage, 'database', side_effect=OSError('private DB path')):
            response = self.client.get('/api/service-status')
        self.assertEqual(response.json()['database']['state'], 'disconnected')
        self.assertNotIn('private DB path', response.text)
        self.assertEqual(self.client.get('/api/service-status?space_id=other').status_code, 422)

    def test_real_probe_cache_space_isolation_and_config_change(self):
        config = self.configure()
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(str(request.url), 'https://provider.example/v1/models')
            self.assertEqual(request.headers['Authorization'], 'Bearer private-secret')
            return httpx.Response(200, json={'data': [{'id': 'test-model'}]})
        with patch.object(service_status.httpx, 'AsyncClient', side_effect=lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)):
            for _ in range(2):
                response = self.client.get('/api/service-status')
                self.assertEqual(response.json()['model_api']['state'], 'connected')
                self.assertNotIn('private-secret', response.text)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.client.get('/api/service-status?space_id=roaming').json()['model_api']['state'], 'unconfigured')
            self.client.put('/api/model-configs/'+config['id'], json={'name':'修改', 'base_url':'https://provider.example/v1', 'model':'another-model'})
            self.assertEqual(self.client.get('/api/service-status').json()['model_api']['state'], 'model_unlisted')
            self.assertEqual(len(calls), 2)

    def test_provider_failures_never_show_connected(self):
        self.configure()
        for status, body, expected in [
            (401, {}, 'unauthorized'), (403, {}, 'unauthorized'),
            (404, {}, 'unverified'), (405, {}, 'unverified'),
            (429, {}, 'limited'), (500, {}, 'disconnected'),
            (200, {}, 'unverified'), (200, {'data': ['bad']}, 'unverified'),
            (200, {'data': []}, 'model_unlisted'),
        ]:
            with self.subTest(status=status, body=body):
                service_status._cache.clear()
                with patch.object(service_status.httpx, 'AsyncClient', side_effect=lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)), **kw)):
                    data = self.client.get('/api/service-status').json()
                self.assertEqual(data['database']['state'], 'connected')
                self.assertEqual(data['model_api']['state'], expected)

    def test_timeout(self):
        self.configure()
        def handler(request):
            raise httpx.ConnectTimeout('private details', request=request)
        with patch.object(service_status.httpx, 'AsyncClient', side_effect=lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)):
            response = self.client.get('/api/service-status')
        self.assertEqual(response.json()['model_api']['state'], 'disconnected')
        self.assertNotIn('private details', response.text)
