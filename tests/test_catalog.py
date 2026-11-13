import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from vivideo import catalog_store, storage
from vivideo.app import app


CREATED = 1704038400000
TAGS = {
    '数据库': '11111111-1111-4111-8111-111111111111',
    '入门': '22222222-2222-4222-8222-222222222222',
    '实战': '33333333-3333-4333-8333-333333333333',
}
VIDEOS = (
    ('a', 'SQL 入门', '示例作者', 'ready'),
    ('b', '事务实战', '示例作者', 'ready'),
    ('c', '威尼斯散步', '漫游作者', 'failed'),
    ('d', None, None, None),
    ('e', '已清理样例', '示例作者', 'evicted'),
)
ASSIGNMENTS = (
    ('a', '数据库'), ('a', '入门'), ('b', '数据库'), ('b', '实战'),
    ('c', '入门'), ('e', '数据库'), ('e', '入门'),
)


def video_id(letter):
    return 'demo-video-' + letter


def seed_catalog(space='learning'):
    with storage.database(space) as db:
        catalog_store.ensure_schema(db)
        db.executemany(
            'INSERT INTO videos(id, url, query, created) VALUES (?, ?, ?, ?)',
            [(video_id(letter), f'https://example.com/video/{letter}', 'fixture', CREATED)
             for letter, *_ in VIDEOS],
        )
        db.executemany(
            'INSERT INTO media_assets(video_id, title, author, state, duration) VALUES (?, ?, ?, ?, ?)',
            [(video_id(letter), title, author, state, 100)
             for letter, title, author, state in VIDEOS if state is not None],
        )
        db.executemany(
            'INSERT INTO catalog_tags(id, name, created, updated) VALUES (?, ?, ?, ?)',
            [(tag_id, name, CREATED, CREATED) for name, tag_id in TAGS.items()],
        )
        db.executemany(
            'INSERT INTO catalog_video_tags(video_id, tag_id) VALUES (?, ?)',
            [(video_id(letter), TAGS[name]) for letter, name in ASSIGNMENTS],
        )


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {'VIVIDEO_DATA_DIR': temp}))
        self.client = self.enterContext(TestClient(app))
        self.base = '/api/spaces/learning'
        self.tags_url = self.base + '/video-tags'
        self.catalog_url = self.base + '/video-catalog'
        seed_catalog()

    def get_catalog(self, **params):
        response = self.client.get(self.catalog_url, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_videos(self, letters, **params):
        data = self.get_catalog(**params)
        self.assertEqual([item['video_id'] for item in data['items']], [video_id(letter) for letter in letters])
        return data

    def set_tags(self, letter, *tag_ids):
        return self.client.put(f'{self.catalog_url}/{video_id(letter)}/tags', json={'tag_ids': tag_ids})

    def list_tags(self):
        response = self.client.get(self.tags_url)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['items']

    def test_tag_lifecycle_and_visible_counts(self):
        self.assertEqual(
            {tag['name']: tag['video_count'] for tag in self.list_tags()},
            {'数据库': 2, '入门': 2, '实战': 1},
        )
        with patch('vivideo.catalog_store.time_ns', return_value=(CREATED + 1) * 1_000_000):
            response = self.client.post(self.tags_url, json={'name': '\u3000 SQL \t'})
        self.assertEqual(response.status_code, 201)
        tag = response.json()
        UUID(tag['id'])
        self.assertEqual(tag, {
            'id': tag['id'], 'name': 'SQL', 'created': CREATED + 1,
            'updated': CREATED + 1, 'video_count': 0,
        })
        self.assertEqual(self.client.post(self.tags_url, json={'name': 'sql'}).status_code, 201)
        self.assertEqual(self.client.post(self.tags_url, json={'name': ' SQL '}).status_code, 409)
        path = f'{self.tags_url}/{TAGS["数据库"]}'
        self.assertEqual(self.client.patch(path, json={'name': '数据库'}).status_code, 200)
        self.assertEqual(self.client.patch(path, json={'name': '入门'}).status_code, 409)
        self.assertIn('数据库', {tag['name'] for tag in self.list_tags()})

        with patch('vivideo.catalog_store.time_ns', return_value=(CREATED + 2) * 1_000_000):
            renamed = self.client.patch(path, json={'name': ' 关系数据库 '})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json(), {
            'id': TAGS['数据库'], 'name': '关系数据库', 'video_count': 2,
            'created': CREATED, 'updated': CREATED + 2,
        })
        # A fresh client and database connection read the saved classification.
        with TestClient(app) as other_client:
            catalog = other_client.get(self.catalog_url, params={'tag_ids': TAGS['数据库']}).json()
        self.assertEqual(catalog['total'], 2)
        self.assertIn('关系数据库', [tag['name'] for tag in catalog['items'][0]['tags']])
        self.assertEqual([tag['name'] for tag in self.list_tags()], ['SQL', 'sql', '入门', '关系数据库', '实战'])

    def test_delete_cascades_only_catalog_associations(self):
        with storage.database('learning') as db:
            db.execute('UPDATE media_assets SET pinned=1 WHERE video_id=?', (video_id('a'),))
            db.execute(
                'INSERT INTO watch_events VALUES (?, ?, NULL, 30, 100, ?, ?)',
                (str(uuid4()), video_id('a'), CREATED, CREATED),
            )
            # Simulate a separate module owning data linked to the same video.
            db.execute('CREATE TABLE sample_notes(video_id TEXT REFERENCES videos(id))')
            db.execute('INSERT INTO sample_notes VALUES (?)', (video_id('a'),))
        path = f'{self.tags_url}/{TAGS["数据库"]}'
        response = self.client.delete(path)
        self.assertEqual((response.status_code, response.content), (204, b''))
        self.assertEqual(self.client.delete(path).status_code, 404)
        self.assertEqual(self.client.patch(path, json={'name': '不存在'}).status_code, 404)
        data = self.assert_videos('a', q='SQL')
        self.assertEqual([tag['name'] for tag in data['items'][0]['tags']], ['入门'])
        with storage.database('learning') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM videos').fetchone()[0], 5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM watch_events').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sample_notes').fetchone()[0], 1)
            self.assertEqual(tuple(db.execute(
                'SELECT state, pinned FROM media_assets WHERE video_id=?', (video_id('a'),)
            ).fetchone()), ('ready', 1))
            self.assertEqual(db.execute(
                'SELECT COUNT(*) FROM catalog_video_tags WHERE tag_id=?', (TAGS['数据库'],)
            ).fetchone()[0], 0)

    def test_replacement_and_clear_are_idempotent(self):
        for _ in range(2):
            response = self.set_tags('a', TAGS['实战'], TAGS['实战'], TAGS['入门'])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {'video_id': video_id('a'), 'tags': [
                {'id': TAGS['入门'], 'name': '入门'}, {'id': TAGS['实战'], 'name': '实战'},
            ]})
        self.assert_videos('b', tag_ids=TAGS['数据库'])
        self.assertEqual(self.set_tags('a').json(), {'video_id': video_id('a'), 'tags': []})
        self.assert_videos('ad', untagged=True)

    def test_unknown_tag_does_not_replace_existing_tags(self):
        original = self.assert_videos('a', q='SQL')['items'][0]['tags']
        response = self.set_tags('a', TAGS['实战'], str(uuid4()))
        self.assertEqual(response.status_code, 404)
        self.assertIsInstance(response.json()['detail'], str)
        self.assertEqual(self.assert_videos('a', q='SQL')['items'][0]['tags'], original)
        self.assertEqual(self.set_tags('z', TAGS['数据库']).status_code, 404)

    def test_replacement_rolls_back_when_insertion_fails(self):
        with storage.database('learning') as db:
            db.execute("""CREATE TRIGGER reject_assignment BEFORE INSERT ON catalog_video_tags
                          BEGIN SELECT RAISE(ABORT, 'forced assignment failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'forced assignment failure'):
            self.set_tags('a', TAGS['实战'])
        self.assert_videos('a', tag_ids=[TAGS['数据库'], TAGS['入门']])
        self.assert_videos('b', tag_ids=TAGS['实战'])

    def test_fixed_sample_filters_and_case_sensitive_search(self):
        both = [TAGS['数据库'], TAGS['入门']]
        self.assertEqual(self.assert_videos('abcd')['total'], 4)
        self.assert_videos('a', tag_ids=both)
        self.assert_videos('a', tag_ids=[*both, both[0]], tag_mode='all')
        self.assertEqual(self.assert_videos('abc', tag_ids=both, tag_mode='any')['total'], 3)
        self.assert_videos('d', untagged=True)
        self.assert_videos('ab', tag_ids=TAGS['数据库'], cache_state='ready')
        self.assert_videos('b', tag_ids=TAGS['数据库'], q='　事务 ')
        self.assert_videos('ab', cache_state='ready', q='示例作者')
        self.assert_videos('', q='sql')
        self.assert_videos('', q='已清理')
        self.assertEqual(self.set_tags('e', TAGS['数据库']).status_code, 404)

    def test_pagination_returns_complete_tags_and_stable_order(self):
        both = [TAGS['数据库'], TAGS['入门']]
        first = self.assert_videos('a', tag_ids=both, tag_mode='any', limit=1, offset=0)
        second = self.assert_videos('b', tag_ids=both, tag_mode='any', limit=1, offset=1)
        self.assertEqual([tag['name'] for tag in second['items'][0]['tags']], ['实战', '数据库'])
        self.assertEqual([first['total'], second['total']], [3, 3])
        self.assertEqual((second['limit'], second['offset']), (1, 1))
        self.assertEqual(self.assert_videos('', offset=4)['total'], 4)
        self.assertEqual(self.assert_videos('', offset=2**63)['total'], 4)
        with storage.database('learning') as db:
            db.execute('UPDATE videos SET created=created+1 WHERE id=?', (video_id('c'),))
        self.assert_videos('cabd')

    def test_missing_asset_defaults_and_can_be_classified(self):
        data = self.assert_videos('d', cache_state='missing')['items'][0]
        self.assertEqual(data, {
            'video_id': video_id('d'), 'url': 'https://example.com/video/d', 'created': CREATED,
            'title': video_id('d'), 'author': '', 'duration_seconds': None,
            'cache_state': 'missing', 'tags': [],
        })
        self.assertEqual(self.set_tags('d', TAGS['数据库']).status_code, 200)
        self.assert_videos('d', tag_ids=TAGS['数据库'], cache_state='missing')
        tag = next(tag for tag in self.list_tags() if tag['id'] == TAGS['数据库'])
        self.assertEqual(tag['video_count'], 3)
        with storage.database('learning') as db:
            db.execute('UPDATE media_assets SET state=? WHERE video_id=?', ('ready', video_id('e')))
        self.assert_videos('ae', tag_ids=[TAGS['数据库'], TAGS['入门']])

    def test_spaces_are_isolated(self):
        roaming_tags = '/api/spaces/roaming/video-tags'
        roaming_catalog = '/api/spaces/roaming/video-catalog'
        response = self.client.post(roaming_tags, json={'name': '数据库'})
        self.assertEqual(response.status_code, 201)
        other_id = response.json()['id']
        self.assertNotEqual(other_id, TAGS['数据库'])
        self.assertEqual(self.client.get(roaming_catalog).json()['total'], 0)
        self.assertEqual(self.client.get(self.catalog_url, params={'tag_ids': other_id}).status_code, 404)
        self.assertEqual(self.client.get(roaming_catalog, params={'tag_ids': TAGS['数据库']}).status_code, 404)
        self.assertEqual(self.set_tags('a', other_id).status_code, 404)
        self.assertEqual(self.client.delete(f'{roaming_tags}/{TAGS["数据库"]}').status_code, 404)
        self.assertEqual(self.client.put(
            f'{roaming_catalog}/{video_id("a")}/tags', json={'tag_ids': [other_id]},
        ).status_code, 404)

    def test_video_id_with_slash_can_be_classified(self):
        custom_id = 'existing/video'
        with storage.database('learning') as db:
            db.execute(
                'INSERT INTO videos(id, url, query, created) VALUES (?, ?, ?, ?)',
                (custom_id, 'https://example.com/existing/video', 'fixture', CREATED),
            )
        encoded_id = quote(custom_id, safe='')
        response = self.client.put(
            f'{self.catalog_url}/{encoded_id}/tags', json={'tag_ids': [TAGS['入门']]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['video_id'], custom_id)
        data = self.get_catalog(q=custom_id, tag_ids=TAGS['入门'])
        self.assertEqual([item['video_id'] for item in data['items']], [custom_id])

    def test_validation(self):
        for name in ['', '　\t', '长' * 31, '\x00', 'a\x00b']:
            with self.subTest(name=name):
                self.assertEqual(self.client.post(self.tags_url, json={'name': name}).status_code, 422)
        self.assertEqual(self.client.post(self.tags_url, json={'name': '长' * 30 + ' '}).status_code, 201)
        self.assertEqual(self.client.post(self.tags_url, json={'name': 'valid', 'other': 1}).status_code, 422)
        path = f'{self.tags_url}/{TAGS["数据库"]}'
        self.assertEqual(self.client.patch(path, json={'name': 'a\x00b'}).status_code, 422)
        self.assertEqual(self.client.delete(f'{self.tags_url}/invalid').status_code, 422)
        self.assertEqual(self.client.get('/api/spaces/invalid/video-catalog').status_code, 422)
        invalid_queries = (
            {'q': '字' * 101}, {'limit': 0}, {'limit': 101}, {'offset': -1},
            {'cache_state': 'evicted'}, {'tag_mode': 'some'}, {'tag_ids': 'invalid'},
            {'tag_ids': TAGS['入门'], 'untagged': True}, {'tag_ids': [TAGS['入门']] * 21},
        )
        for params in invalid_queries:
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.catalog_url, params=params).status_code, 422)
        self.assertEqual(self.get_catalog(q=' ' + '字' * 100 + ' ')['total'], 0)
        self.assertEqual(self.get_catalog(limit=100)['limit'], 100)
        self.assertEqual(self.set_tags('a', 'invalid').status_code, 422)
        self.assertEqual(self.set_tags('a', *([TAGS['入门']] * 21)).status_code, 422)
        self.assertEqual(self.set_tags('a', *([TAGS['入门']] * 20)).status_code, 200)
        self.assertEqual(self.client.put(
            f'{self.catalog_url}/{video_id("a")}/tags', json={'tag_ids': [], 'extra': True},
        ).status_code, 422)
        self.assertEqual(self.client.put(
            f'{self.catalog_url}/{"x" * 101}/tags', json={'tag_ids': []},
        ).status_code, 422)

    def test_special_characters_are_literal(self):
        special = """<script>alert('x')</script>%_"""
        response = self.client.post(self.tags_url, json={'name': special})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['name'], special)
        self.assertEqual(self.set_tags('a', response.json()['id']).status_code, 200)
        with storage.database('learning') as db:
            db.execute('UPDATE media_assets SET title=? WHERE video_id=?', (special, video_id('a')))
        for query in (special, '%', '_', "'"):
            self.assert_videos('a', q=query)
        self.assert_videos('', q="' OR 1=1 --")

    def test_static_page_is_served_by_gateway(self):
        for name in ('part-a.html', 'part-a.js', 'part-a.css'):
            self.assertEqual(self.client.get('/static/' + name).status_code, 200)
