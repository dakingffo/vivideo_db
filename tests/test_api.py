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
