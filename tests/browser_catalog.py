"""Real gateway/browser acceptance for the catalog integrated into My Library.
Run with PYTHONPATH=python python tests/browser_catalog.py (Playwright Chromium required).
"""
import asyncio
import base64
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

import httpx
from playwright.async_api import async_playwright
from vivideo import storage, library

ROOT=Path(__file__).resolve().parents[1]

async def main():
    with tempfile.TemporaryDirectory() as folder:
        env={**os.environ,'VIVIDEO_DATA_DIR':folder,'PYTHONPATH':str(ROOT/'python')}
        with patch.dict(os.environ,env):
            assets=library.root('learning')/'assets'/'browser'
            assets.mkdir(parents=True)
            (assets/'video.mp4').write_bytes(b'cache fixture')
            (assets/'cover.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1kAAAAASUVORK5CYII='))
            with storage.database('learning') as db:
                db.executemany('INSERT INTO videos VALUES (?,?,?,?)',[('browser-ready','https://example.com/a','',1),('browser-missing','https://example.com/b','',2)])
                db.execute("INSERT INTO media_assets(video_id,title,author,state,duration,relative_path,thumbnail_path,bytes,pinned) VALUES ('browser-ready','SQL 入门','示例作者','ready',100,'assets/browser/video.mp4','assets/browser/cover.png',80,1)")
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        with open(Path(folder)/'gateway.log','w') as log:
            process=subprocess.Popen([sys.executable,'-m','uvicorn','vivideo.app:app','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                async with httpx.AsyncClient() as client:
                    for _ in range(100):
                        try:
                            if (await client.get(base+'/')).status_code==200:break
                        except httpx.HTTPError:pass
                        await asyncio.sleep(.1)
                async with async_playwright() as pw:
                    browser=await pw.chromium.launch(headless=True)
                    context=await browser.new_context(viewport={'width':1440,'height':1000},reduced_motion='reduce')
                    page=await context.new_page()
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    await page.goto(base)
                    await page.wait_for_function('ready')
                    await page.locator('[data-page=library]').click()
                    await page.wait_for_function('document.querySelectorAll(".catalog-card").length===2')
                    assert await page.locator('#local-cache-list').count()==0
                    assert await page.locator('#search').evaluate('(el)=>getComputedStyle(el).borderTopWidth')=='0px'
                    assert await page.locator('#cache-state').evaluate('(el)=>getComputedStyle(el).appearance')=='none'
                    assert await page.locator('#import-video-form').is_visible()
                    assert await page.locator('#cache-quota-form').is_visible()
                    await page.locator('#manage-catalog-tags').click()
                    await page.locator('#new-tag').fill('数据库')
                    await page.locator('#create-button').click()
                    await page.wait_for_selector('#managed-tags li')
                    await page.locator('#managed-tags button').first.click()
                    await page.locator('#rename-tag').fill('数据库知识')
                    await page.locator('#confirm-tag-action').click()
                    await page.wait_for_function('!document.querySelector("#tag-dialog").open')
                    await page.locator('#close-catalog-manager').click()
                    card=page.locator('[data-catalog-id=browser-ready]')
                    await card.locator('[data-edit-tags]').click()
                    await page.locator('#video-tag-choices input').check()
                    await page.locator('#save-video-tags').click()
                    await page.wait_for_function('!document.querySelector("#video-dialog").open')
                    await page.wait_for_function('document.querySelector("[data-catalog-id=browser-ready] .card-tags").textContent.includes("数据库")')
                    await page.locator('#filter-tags input').check()
                    await page.wait_for_function('document.querySelectorAll(".catalog-card").length===1')
                    await page.locator('#filter-tags input').uncheck()
                    await page.wait_for_function('document.querySelectorAll(".catalog-card").length===2')
                    await page.locator('#search').fill('不存在的标题')
                    await page.locator('#search-form button').click()
                    await page.wait_for_selector('#library-grid .empty')
                    await page.locator('#search').fill('')
                    await page.locator('#search-form button').click()
                    await page.wait_for_selector('[data-catalog-id=browser-ready]')
                    await page.evaluate('window.scrollTo(0,0)')
                    await page.screenshot(path='/tmp/vivideo-integrated-catalog.png',full_page=True)
                    await page.set_viewport_size({'width':390,'height':844})
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    await page.screenshot(path='/tmp/vivideo-integrated-catalog-mobile.png',full_page=True)
                    await page.set_viewport_size({'width':1440,'height':1000})
                    await card.locator('[data-video-note]').click()
                    await page.wait_for_function('document.querySelector("#video-notes-name").disabled===false')
                    await page.locator('#video-notes-name').fill('事务笔记')
                    await page.locator('#video-notes-content').fill('原子性与隔离性 <script>bad()</script>')
                    await page.locator('#save-video-note').click()
                    await page.wait_for_function('document.querySelector("#video-notes-status").textContent.includes("版本 1")')
                    other=await page.context.new_page()
                    await other.goto(base);await other.wait_for_function('ready')
                    await other.locator('[data-page=library]').click()
                    await other.locator('[data-catalog-id=browser-ready] [data-video-note]').click()
                    await other.wait_for_function('document.querySelector("#video-notes-content").value.includes("原子性")')
                    await page.locator('#video-notes-content').fill('新版本的正文')
                    await page.locator('#save-video-note').click()
                    await page.wait_for_function('document.querySelector("#video-notes-status").textContent.includes("版本 2")')
                    await other.locator('#video-notes-content').fill('不能覆盖的草稿')
                    await other.locator('#save-video-note').click()
                    await other.wait_for_function('document.querySelector("#video-notes-status").textContent.includes("其他页面")')
                    assert await other.locator('#video-notes-content').input_value()=='不能覆盖的草稿'
                    other.once('dialog',lambda dialog:dialog.dismiss())
                    await other.locator('#close-video-notes').click()
                    assert await other.locator('#video-notes-dialog').is_visible()
                    other.once('dialog',lambda dialog:dialog.accept())
                    await other.locator('#reload-video-notes').click()
                    await other.wait_for_function('document.querySelector("#video-notes-content").value==="新版本的正文"')
                    await other.close()
                    async def fail_note_save(route):
                        if route.request.method=='PUT':await route.abort()
                        else:await route.continue_()
                    await page.route('**/api/spaces/learning/notes/*',fail_note_save)
                    await page.locator('#video-notes-content').fill('网络失败也保留正文')
                    await page.locator('#save-video-note').click()
                    await page.wait_for_function('document.querySelector("#video-notes-status").textContent.includes("连接失败")')
                    assert await page.locator('#video-notes-content').input_value()=='网络失败也保留正文'
                    await page.unroute('**/api/spaces/learning/notes/*',fail_note_save)
                    await page.locator('#save-video-note').click()
                    await page.wait_for_function('document.querySelector("#video-notes-status").textContent.includes("版本 3")')
                    await page.locator('#video-note-versions summary').click()
                    await page.wait_for_function('document.querySelectorAll("#video-note-revisions details").length===3')
                    await page.locator('#close-video-notes').click()
                    await card.locator('[data-video-note]').click()
                    await page.wait_for_function('document.querySelector("#video-notes-content").value==="网络失败也保留正文"')
                    await page.set_viewport_size({'width':390,'height':844})
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    await page.screenshot(path='/tmp/vivideo-video-notes-mobile.png',full_page=True)
                    await page.set_viewport_size({'width':1440,'height':1000})
                    await page.locator('#new-video-note').click()
                    await page.locator('#video-notes-name').fill('第二条笔记')
                    await page.locator('#video-notes-content').fill('独立的想法')
                    await page.locator('#save-video-note').click()
                    await page.wait_for_function('document.querySelectorAll("#video-notes-select option").length===3')
                    page.once('dialog',lambda dialog:dialog.accept())
                    await page.locator('#delete-video-note').click()
                    await page.wait_for_function('document.querySelectorAll("#video-notes-select option").length===2')
                    await page.locator('#close-video-notes').click()
                    page.once('dialog',lambda dialog: dialog.accept())
                    await card.locator('[data-catalog-action=delete]').click()
                    await page.wait_for_function('!document.querySelector("[data-catalog-id=browser-ready]")')
                    assert not assets.exists(), 'cache folder should be removed with directory card'
                    await page.locator('#space-toggle').click()
                    await page.locator('#space-options [data-space=roaming]').click()
                    await page.wait_for_function('spaceId==="roaming"&&!spaceTransition')
                    await page.locator('[data-page=library]').click()
                    await page.wait_for_selector('#library-grid .empty')
                    assert await page.locator('.catalog-card').count()==0
                    await page.set_viewport_size({'width':390,'height':844})
                    await page.screenshot(path='/tmp/vivideo-integrated-catalog-roaming-mobile.png',full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert not errors,errors
                    assert (await page.request.get(base+'/static/part-a.html')).status==404
                    for suffix in ['html','css','js']:
                        assert (await page.request.get(base+'/static/part-b.'+suffix)).status==404
                    await browser.close()
                print('Integrated catalog and video note popup passed: real API, shared style/cards, tags, search/filter, delete with pinned cache, space isolation, mobile and retired standalone page.')
            finally:
                process.terminate();process.wait(timeout=10)

if __name__=='__main__':asyncio.run(main())
