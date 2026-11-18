import unittest
from fastapi.testclient import TestClient
from vivideo.app import app
from unittest.mock import patch

class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.mode = patch('vivideo.app.MODE', 'demo')
        self.mode.start()
        self.addCleanup(self.mode.stop)
        self.client = TestClient(app)

    def test_ui_and_assets(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/static/app.js').status_code, 200)

    def test_demo_contract(self):
        result = self.client.post('/api/chat', json={'message': 'AI Agent 入门', 'personalized': False, 'preferences': []})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['demo'])
        self.assertEqual(len(result.json()['videos']), 5)

    def test_empty_message_rejected(self):
        for message in ['', '   ']:
            self.assertEqual(self.client.post('/api/chat', json={'message': message}).status_code, 422)

    def test_frontend_cache_is_revalidated_and_main_script_is_versioned(self):
        page = self.client.get('/')
        self.assertEqual(page.headers['cache-control'], 'no-cache')
        self.assertIn('/static/app.js?v=library-5', page.text)
        asset = self.client.get('/static/app.js?v=library-5')
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.headers['cache-control'], 'no-cache')
        if asset.headers.get('etag'):
            cached = self.client.get('/static/app.js?v=library-5', headers={'If-None-Match':asset.headers['etag']})
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.headers['cache-control'], 'no-cache')
