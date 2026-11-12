import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from vivideo.app import app


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'VIVIDEO_DATA_DIR': self.directory.name})
        self.env.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def create(self, **changes):
        payload = dict(name='日常模型', base_url='https://example.com/v1', model='test-model', api_key='private-test-secret')
        payload.update(changes)
        response = self.client.post('/api/model-configs', json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertNotIn('private-test-secret', response.text)
        return response.json()

    def test_persistence_selection_and_secret_update(self):
        config = self.create()
        config_id = config['id']
        self.assertTrue(config['has_api_key'])
        self.assertEqual(self.client.put('/api/model-configs/selection/learning', json={'config_id': config_id}).status_code, 200)
        # A fresh client sees persistent records, but each space has its own selection.
        client = TestClient(app)
        learning = client.get('/api/model-configs?space_id=learning').json()
        roaming = client.get('/api/model-configs?space_id=roaming').json()
        self.assertEqual(learning['selected_id'], config_id)
        self.assertIsNone(roaming['selected_id'])
        self.assertEqual(learning['configs'], roaming['configs'])
        self.assertNotIn('api_key', learning['configs'][0])
        payload = dict(name='新名称', base_url='https://example.com/v2', model='new-model')
        self.assertEqual(client.put('/api/model-configs/'+config_id, json=payload).status_code, 200)
        with sqlite3.connect(Path(self.directory.name)/'settings.db') as db:
            self.assertEqual(db.execute('SELECT api_key FROM model_configs').fetchone()[0], 'private-test-secret')
        payload['api_key'] = 'replacement'
        client.put('/api/model-configs/'+config_id, json=payload)
        with sqlite3.connect(Path(self.directory.name)/'settings.db') as db:
            self.assertEqual(db.execute('SELECT api_key FROM model_configs').fetchone()[0], 'replacement')
        payload['api_key'] = ''
        self.assertFalse(client.put('/api/model-configs/'+config_id, json=payload).json()['has_api_key'])
        self.assertEqual((Path(self.directory.name)/'settings.db').stat().st_mode & 0o777, 0o600)
        self.assertEqual(client.delete('/api/model-configs/'+config_id).status_code, 204)
        self.assertEqual(client.get('/api/model-configs').json(), {'configs': [], 'selected_id': None})

    def test_validation_and_unknown_selection(self):
        for base_url in ['file:///tmp/key', 'https://user:secret@example.com/v1', 'https://example.com:bad/v1']:
            result = self.client.post('/api/model-configs', json=dict(name='test', model='test', base_url=base_url))
            self.assertEqual(result.status_code, 422)
        self.assertEqual(self.client.put('/api/model-configs/selection/learning', json={'config_id': 'missing'}).status_code, 404)
        self.assertEqual(self.client.get('/api/model-configs?space_id=unknown').status_code, 422)
