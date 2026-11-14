"""Part B acceptance tests. Fixtures are metadata only; no external requests."""
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from vivideo import notes_store, storage
from vivideo.notes_api import router


def seed():
    for space in ('learning', 'roaming'):
        with storage.database(space) as db:
            for suffix, title, duration in [('a', 'SQL 入门', 100), ('b', '事务基础', 120)]:
                vid = f'demo-video-{suffix}'
                db.execute('INSERT INTO videos(id,url,query,created) VALUES (?,?,?,?)',
                           (vid, f'https://example.com/video/{suffix}', 'fixture', 1704038400000))
                db.execute('INSERT INTO media_assets(video_id,title,author,duration) VALUES (?,?,?,?)',
                           (vid, title if space == 'learning' else '漫游视频', 'fixture', duration))


class NotesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, VIVIDEO_DATA_DIR=temporary.name)
        env.start()
        self.addCleanup(env.stop)
        seed()
        app = FastAPI()
        app.include_router(router)

        @app.exception_handler(storage.StateError)
        async def state_error(request, exc):
            return JSONResponse(status_code=exc.status, content={'detail': str(exc)})

        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.base = '/api/spaces/learning/notes'

    def create(self, **changes):
        payload = dict(video_id='demo-video-a', title='事务的例子', content='第一行\n第二行')
        payload.update(changes)
        response = self.client.post(self.base, json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def edit(self, note, **changes):
        payload = dict(title=note['title'], content=note['content'], expected_version=note['version'])
        payload.update(changes)
        return self.client.put(f"{self.base}/{note['id']}", json=payload)

    def test_create_duplicates_and_persist_with_new_connection(self):
        first = self.create(title='  同名标题 \n', content=' \n正文\n ')
        second = self.create(title='同名标题')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['title'], '同名标题')
        self.assertEqual(first['content'], ' \n正文\n ')
        self.assertEqual(first['video_title'], 'SQL 入门')
        self.assertEqual(first['version'], 1)
        self.assertEqual(first['created'], first['updated'])
        self.assertIsInstance(first['created'], int)
        # New client/connection reads the committed data, without module state.
        with TestClient(self.client.app) as restarted:
            self.assertEqual(restarted.get(f"{self.base}/{first['id']}").json(), first)
        self.assertEqual(self.client.get(self.base).json()['total'], 2)

    def test_revision_snapshots_include_current_and_unchanged_saves(self):
        note = self.create()
        note = self.edit(note, title='版本二', content='第二次').json()
        self.assertEqual(note['version'], 2)
        note = self.edit(note).json()
        self.assertEqual(note['version'], 3)
        history = self.client.get(f"{self.base}/{note['id']}/revisions").json()
        self.assertEqual(history['total'], 3)
        self.assertEqual([r['version'] for r in history['items']], [3, 2, 1])
        self.assertEqual(history['items'][0]['content'], '第二次')
        self.assertEqual(history['items'][0]['saved'], note['updated'])
        page = self.client.get(f"{self.base}/{note['id']}/revisions?limit=1&offset=1").json()
        self.assertEqual(page['total'], 3)
        self.assertEqual(page['items'], [history['items'][1]])

    def test_stale_client_conflict_leaves_note_and_history_unchanged(self):
        old = self.create()
        saved = self.edit(old, content='获胜内容').json()
        failed = self.edit(old, content='不应覆盖')
        self.assertEqual(failed.status_code, 409)
        self.assertIn('detail', failed.json())
        self.assertEqual(self.client.get(f"{self.base}/{old['id']}").json(), saved)
        history = self.client.get(f"{self.base}/{old['id']}/revisions").json()
        self.assertEqual([r['content'] for r in history['items']], ['获胜内容', old['content']])

    def test_simultaneous_writers_only_one_succeeds(self):
        note = self.create()
        barrier = Barrier(2)

        def save(content):
            barrier.wait()
            try:
                return notes_store.update_note('learning', note['id'], '并发保存', content, 1)['version']
            except storage.StateError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ['客户端 A', '客户端 B']))
        self.assertCountEqual(results, [2, 409])
        self.assertEqual(notes_store.list_revisions('learning', note['id'])['total'], 2)

    def test_snapshot_failure_rolls_back_create_and_update(self):
        note = self.create()
        # SQLite trigger fails the actual INSERT after latest-note UPDATE has run.
        with storage.database('learning') as db:
            notes_store.ensure_schema(db)
            db.execute('''CREATE TRIGGER fail_revision BEFORE INSERT ON note_revisions
                BEGIN SELECT RAISE(ABORT, 'injected snapshot failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            notes_store.update_note('learning', note['id'], '不能留下', '不能留下', 1)
        with self.assertRaises(sqlite3.IntegrityError):
            notes_store.create_note('learning', 'demo-video-a', '不能留下', '')
        self.assertEqual(notes_store.get_note('learning', note['id']), note)
        self.assertEqual(notes_store.list_notes('learning')['total'], 1)
        self.assertEqual(notes_store.list_revisions('learning', note['id'])['total'], 1)

    def test_schema_idempotent_does_not_commit_existing_transaction(self):
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with storage.database('learning') as db:
                notes_store.ensure_schema(db)
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE videos SET query = 'uncommitted' WHERE id = 'demo-video-a'")
                notes_store.ensure_schema(db)
                notes_store.ensure_schema(db)
                raise RuntimeError('rollback')
        self.assertEqual(storage.get_video('learning', 'demo-video-a')['query'], 'fixture')

    def test_validation_create(self):
        base = dict(video_id='demo-video-a', title='正常', content='')
        invalid = [dict(title=''), dict(title=' \n\t'), dict(title='字' * 81),
                   dict(content='字' * 10001), dict(content=None), dict(video_id=''),
                   dict(video_id='v' * 101), dict(video_id=42), dict(unknown='x')]
        for change in invalid:
            with self.subTest(change=str(change)[:100]):
                self.assertEqual(self.client.post(self.base, json={**base, **change}).status_code, 422)
        del base['content']
        self.assertEqual(self.client.post(self.base, json=base).status_code, 422)
        note = self.create(title='字' * 80, content='字' * 10000)
        self.assertEqual(len(note['content']), 10000)
        self.assertEqual(self.create(content='')['content'], '')

    def test_validation_update_and_immutable_video(self):
        note = self.create()
        for version in [0, -1, 1.5, True, '1', None]:
            self.assertEqual(self.edit(note, expected_version=version).status_code, 422)
        for changes in [dict(video_id='demo-video-b'), dict(title='  '),
                        dict(title='x' * 81), dict(content='x' * 10001), dict(content=None)]:
            self.assertEqual(self.edit(note, **changes).status_code, 422)
        for missing in ['content', 'expected_version', 'title']:
            payload = dict(title='test', content='', expected_version=1)
            del payload[missing]
            self.assertEqual(self.client.put(f"{self.base}/{note['id']}", json=payload).status_code, 422)
        self.assertEqual(notes_store.get_note('learning', note['id']), note)

    def test_validation_space_uuid_pagination_and_search(self):
        note = self.create()
        for suffix in ['', f"/{note['id']}/revisions"]:
            for query in ['limit=0', 'limit=101', 'limit=1.5', 'offset=-1', 'offset=no']:
                self.assertEqual(self.client.get(f'{self.base}{suffix}?{query}').status_code, 422)
        for query in [{'q': 'x' * 101}, {'video_id': ''}, {'video_id': 'x' * 101}]:
            self.assertEqual(self.client.get(self.base, params=query).status_code, 422)
        self.assertEqual(self.client.get('/api/spaces/invalid/notes').status_code, 422)
        for method in ['get', 'delete', 'put']:
            kwargs = {'json': dict(title='x', content='', expected_version=1)} if method == 'put' else {}
            self.assertEqual(getattr(self.client, method)(f'{self.base}/bad-id', **kwargs).status_code, 422)
        self.assertEqual(self.client.get(f'{self.base}/bad-id/revisions').status_code, 422)

    def test_search_filter_sort_and_page(self):
        first = self.create(title='标题命中', content='a' * 100)
        second = self.create(video_id='demo-video-b', title='另一个', content='正文命中')
        third = self.create(title='第三条', content='100%_literal')
        # Explicit tie verifies id ASC as well as updated DESC.
        with storage.database('learning') as db:
            db.execute('UPDATE video_notes SET updated=100')
            db.execute('UPDATE video_notes SET updated=200 WHERE id=?', (third['id'],))
        data = self.client.get(self.base, params={'limit': 2, 'offset': 1}).json()
        expected = [third['id'], *sorted([first['id'], second['id']])]
        self.assertEqual([n['id'] for n in data['items']], expected[1:])
        self.assertEqual((data['total'], data['limit'], data['offset']), (3, 2, 1))
        for n in data['items']:
            self.assertNotIn('content', n)
            self.assertLessEqual(len(n['excerpt']), 80)
        self.assertEqual(notes_store.list_notes('learning', q='标题')['items'][0]['excerpt'], 'a' * 80)
        self.assertEqual(self.client.get(self.base, params={'q': ' 命中 '}).json()['total'], 2)
        self.assertEqual(self.client.get(self.base, params={'q': '命中', 'video_id': 'demo-video-b'}).json()['total'], 1)
        self.assertEqual(self.client.get(self.base, params={'q': '%_'}).json()['total'], 1)
        self.assertEqual(self.client.get(self.base, params={'video_id': 'absent'}).json()['total'], 0)
        self.assertEqual(self.client.get(self.base, params={'q': '  ', 'offset': 100}).json()['total'], 3)
        self.assertEqual(self.client.get(self.base, params={'offset': 100}).json()['items'], [])

    def test_delete_cascades_only_own_revisions(self):
        first, second = self.create(), self.create()
        self.edit(first)
        response = self.client.delete(f"{self.base}/{first['id']}")
        self.assertEqual((response.status_code, response.content), (204, b''))
        with storage.database('learning') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM note_revisions WHERE note_id=?', (first['id'],)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM videos').fetchone()[0], 2)
        self.assertEqual(notes_store.get_note('learning', second['id']), second)
        self.assertEqual(notes_store.list_revisions('learning', second['id'])['total'], 1)
        self.assertEqual(self.client.delete(f"{self.base}/{first['id']}").status_code, 404)

    def test_missing_notes_and_space_isolation(self):
        note = self.create()
        for base in [self.base, '/api/spaces/roaming/notes']:
            missing = str(uuid4()) if base == self.base else note['id']
            for suffix in ['', '/revisions']:
                self.assertEqual(self.client.get(f'{base}/{missing}{suffix}').status_code, 404)
            self.assertEqual(self.client.put(f'{base}/{missing}', json=dict(title='x', content='', expected_version=1)).status_code, 404)
            self.assertEqual(self.client.delete(f'{base}/{missing}').status_code, 404)
        self.assertEqual(self.client.get('/api/spaces/roaming/notes').json()['total'], 0)
        self.assertEqual(self.client.post(self.base, json=dict(video_id='absent', title='x', content='')).status_code, 404)
        with storage.database('learning') as db:
            db.execute("INSERT INTO videos VALUES ('learning-only','https://example.com/only','fixture',1)")
        self.assertEqual(self.client.post('/api/spaces/roaming/notes', json=dict(video_id='learning-only', title='x', content='')).status_code, 404)

    def test_live_video_title_fallback_and_cache_cleanup(self):
        note = self.create()
        with storage.database('learning') as db:
            db.execute("UPDATE media_assets SET title='新标题', state='evicted' WHERE video_id='demo-video-a'")
        self.assertEqual(notes_store.get_note('learning', note['id'])['video_title'], '新标题')
        with storage.database('learning') as db:
            db.execute("UPDATE media_assets SET title='' WHERE video_id='demo-video-a'")
        self.assertEqual(notes_store.get_note('learning', note['id'])['video_title'], 'demo-video-a')
        with storage.database('learning') as db:
            db.execute("DELETE FROM media_assets WHERE video_id='demo-video-a'")
        self.assertEqual(notes_store.get_note('learning', note['id'])['video_title'], 'demo-video-a')

    def test_sql_and_script_payloads_are_literal(self):
        payload = "<script>window.notesInjected=true</script>\n'\"; DROP TABLE videos; -- %_"
        note = self.create(title="引号 ' \" <script>", content=payload)
        self.assertEqual(self.client.get(self.base, params={'q': '; DROP TABLE'}).json()['total'], 1)
        self.assertEqual(self.client.get(self.base, params={'q': "' OR 1=1 --"}).json()['total'], 0)
        self.assertEqual(self.client.get(f"{self.base}/{note['id']}").json()['content'], payload)
        self.assertEqual(len(storage.list_videos('learning')), 2)

    def test_app_route_and_static_integration(self):
        from vivideo.app import app
        with TestClient(app) as client:
            self.assertEqual(client.get(self.base).status_code, 200)
            for name in ['part-b.html', 'part-b.js', 'part-b.css']:
                self.assertEqual(client.get(f'/static/{name}').status_code, 200)


@unittest.skipUnless(os.environ.get('VIVIDEO_NOTES_BROWSER') == '1',
                     'set VIVIDEO_NOTES_BROWSER=1 for optional Playwright acceptance')
class NotesBrowserTests(unittest.TestCase):
    def setUp(self):
        import socket
        import threading
        import time
        import uvicorn
        from playwright.sync_api import sync_playwright
        from vivideo.app import app

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, VIVIDEO_DATA_DIR=temporary.name)
        env.start()
        self.addCleanup(env.stop)
        seed()
        self.note = notes_store.create_note('learning', 'demo-video-a', '事务的例子', '版本一的正文')
        self.other = notes_store.create_note('learning', 'demo-video-b', '另一条笔记', '另一个正文')
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.addCleanup(sock.close)
        self.origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()

        def stop_server():
            server.should_exit = True
            thread.join(timeout=5)

        self.addCleanup(stop_server)
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(server.started)
        playwright = sync_playwright().start()
        self.addCleanup(playwright.stop)
        options = {'headless': True}
        if os.environ.get('VIVIDEO_BROWSER_BIN'):
            options['executable_path'] = os.environ['VIVIDEO_BROWSER_BIN']
        self.browser = playwright.chromium.launch(**options)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def open(self, page=None):
        page = page or self.page
        page.goto(self.origin + '/static/part-b.html')
        page.locator('.note-card').filter(has_text='事务的例子').click()
        page.wait_for_function("document.getElementById('title').value === '事务的例子'")
        return page

    def test_two_pages_conflict_network_failure_and_history(self):
        from playwright.sync_api import expect
        a = self.open()
        b = self.open(self.context.new_page())
        a.locator('#content').fill('客户端 A 已成功保存的内容')
        a.locator('#save').click()
        expect(a.locator('#editor-status')).to_have_text('已保存版本 2')
        b.locator('#content').fill('客户端 B 未保存的文字：需要保留\n<script>alert("文本")</script>')
        b.locator('#save').click()
        expect(b.locator('#conflict')).to_be_visible()
        expect(b.locator('#content')).to_have_value('客户端 B 未保存的文字：需要保留\n<script>alert("文本")</script>')
        expect(b.locator('#save')).to_be_disabled()
        screenshot = os.environ.get('VIVIDEO_NOTES_SCREENSHOT')
        if screenshot:
            Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
            b.screenshot(path=screenshot, full_page=True)
        b.locator('#reload').click()
        b.locator('#confirm-no').click()
        expect(b.locator('#conflict')).to_be_visible()
        b.locator('#reload').click()
        b.locator('#confirm-yes').click()
        expect(b.locator('#content')).to_have_value('客户端 A 已成功保存的内容')
        expect(b.locator('.revision')).to_have_count(2)
        b.locator('#content').fill('网络失败也不能丢失')
        b.route('**/notes/*', lambda route: route.abort() if route.request.method == 'PUT' else route.continue_())
        b.locator('#save').click()
        expect(b.locator('#editor-status')).to_have_class('error')
        expect(b.locator('#content')).to_have_value('网络失败也不能丢失')
        expect(b.locator('#save')).to_be_enabled()
        self.assertEqual(notes_store.get_note('learning', self.note['id'])['version'], 2)
        self.assertFalse(self.errors)

    def test_dirty_switches_cancel_delete_and_plain_text(self):
        from playwright.sync_api import expect
        p = self.open()
        payload = '<script>window.notesInjected=true</script>\n引号 " 和单引号 \' '
        p.locator('#content').fill(payload)
        p.locator('.note-card').filter(has_text='另一条笔记').click()
        expect(p.locator('#confirm-dialog')).to_be_visible()
        p.locator('#confirm-no').click()
        expect(p.locator('#content')).to_have_value(payload)
        p.locator('#space').select_option('roaming')
        p.locator('#confirm-no').click()
        expect(p.locator('#space')).to_have_value('learning')
        expect(p.locator('#content')).to_have_value(payload)
        p.locator('#save').click()
        expect(p.locator('#editor-status')).to_have_text('已保存版本 2')
        expect(p.locator('.note-card').filter(has_text='事务的例子')).to_contain_text('<script>')
        p.locator('.revision summary').first.click()
        expect(p.locator('.revision pre').first).to_have_text(payload)
        self.assertIsNone(p.evaluate('window.notesInjected'))
        p.reload()
        p.locator('.note-card').filter(has_text='事务的例子').click()
        expect(p.locator('#content')).to_have_value(payload)
        p.locator('#delete').click()
        p.locator('#confirm-no').click()
        expect(p.locator('#editor')).to_be_visible()
        p.locator('#delete').click()
        p.locator('#confirm-yes').click()
        expect(p.locator('.note-card')).to_have_count(1)
        p.locator('#new').click()
        p.locator('#video').select_option('demo-video-b')
        p.locator('#title').fill('新笔记')
        p.locator('#save').click()
        expect(p.locator('#editor-status')).to_have_text('已保存版本 1')
        p.locator('#space').select_option('roaming')
        expect(p.locator('#list-status')).to_have_text('没有找到笔记。')
        expect(p.locator('#editor')).to_be_hidden()
        self.assertFalse(self.errors)

    def test_late_responses_do_not_replace_editor_or_space(self):
        from playwright.sync_api import expect
        p = self.open()
        # Hold selected requests in the browser; release them after navigation.
        p.evaluate('''() => {
            const original = window.fetch;
            window.delayed = [];
            window.fetch = async (...args) => {
                const response = await original(...args);
                if (String(args[0]).endsWith('/notes/NOTE_ID')) {
                    return new Promise(resolve => window.delayed.push(() => resolve(response)));
                }
                return response;
            };
        }'''.replace('NOTE_ID', self.other['id']))
        p.locator('.note-card').filter(has_text='另一条笔记').click()
        p.wait_for_function('window.delayed.length === 1')
        p.locator('.note-card').filter(has_text='事务的例子').click()
        expect(p.locator('#title')).to_have_value('事务的例子')
        p.evaluate('window.delayed.shift()()')
        p.wait_for_function("document.getElementById('editor-status').textContent === ''")
        expect(p.locator('#title')).to_have_value('事务的例子')
        p.locator('.note-card').filter(has_text='另一条笔记').click()
        p.wait_for_function('window.delayed.length === 1')
        p.locator('#space').select_option('roaming')
        expect(p.locator('#list-status')).to_have_text('没有找到笔记。')
        p.evaluate('window.delayed.shift()()')
        expect(p.locator('#editor')).to_be_hidden()
        expect(p.locator('#space')).to_have_value('roaming')
        self.assertFalse(self.errors)

    def test_empty_library_and_mobile_layout(self):
        from playwright.sync_api import expect
        with storage.database('roaming') as db:
            db.execute('DELETE FROM media_assets')
            db.execute('DELETE FROM videos')
        p = self.open()
        p.set_viewport_size({'width': 390, 'height': 844})
        self.assertTrue(p.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        p.locator('#space').select_option('roaming')
        expect(p.locator('#videos-status')).to_have_text('当前空间没有视频，暂时无法新建笔记。')
        expect(p.locator('#new')).to_be_disabled()
        self.assertFalse(self.errors)


if __name__ == '__main__':
    unittest.main()
