"""Integrated profile history acceptance; isolated data, real gateway, no model required.
Run: PYTHONPATH=python python tests/browser_history.py
"""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

import httpx
from playwright.async_api import async_playwright
from vivideo import storage

ROOT=Path(__file__).resolve().parents[1]

async def main():
    with tempfile.TemporaryDirectory() as folder:
        env={**os.environ,'VIVIDEO_DATA_DIR':folder,'PYTHONPATH':str(ROOT/'python')}
        with patch.dict(os.environ,env):
            now=int(time.time()*1000)
            with storage.database('learning') as db:
                for vid,title,watched,duration in [('a','SQL 入门',30,100),('b','事务与索引 <script>bad()</script>',60,120)]:
                    db.execute('INSERT INTO videos VALUES (?,?,?,?)',(vid,'https://example.com/'+vid,'fixture',now))
                    db.execute('INSERT INTO media_assets(video_id,title,duration) VALUES (?,?,?)',(vid,title,duration))
                    db.execute('INSERT INTO watch_events VALUES (?,?,?,?,?,?,?)',(str(uuid4()),vid,None,watched,duration,now-60000,now-60000))
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
                    page=await browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    await page.goto(base);await page.wait_for_function('ready')
                    await page.locator('[data-page=profile]').click()
                    await page.wait_for_function('document.querySelector("#watch-count").textContent==="2"')
                    assert await page.locator('#watch-total').inner_text()=='1:30'
                    assert await page.locator('#watch-videos').inner_text()=='2'
                    assert await page.locator('#watch-records .watch-record').count()==2
                    assert await page.locator('#watch-daily tbody tr').count()==7
                    assert await page.locator('#watch-ranking li').count()==2
                    assert await page.locator('.profile-panel').bounding_box()
                    assert await page.locator('#space-toggle').is_enabled()
                    await page.locator('#tag-input').fill('数据库')
                    await page.locator('#tag-form button').click()
                    await page.wait_for_selector('#tags [data-trait]')
                    await page.locator('#watch-query').fill('不存在')
                    await page.locator('#watch-search-form button').click()
                    await page.wait_for_selector('#watch-records .watch-empty')
                    assert await page.locator('#watch-count').inner_text()=='2'
                    await page.locator('#watch-query').fill('')
                    await page.locator('#watch-search-form button').click()
                    await page.wait_for_selector('.watch-delete')
                    await page.locator('[data-watch-days="30"]').click()
                    await page.wait_for_function('document.querySelectorAll("#watch-daily tbody tr").length===30')
                    await page.locator('[data-watch-days="7"]').click()
                    await page.wait_for_function('document.querySelectorAll("#watch-daily tbody tr").length===7')
                    await page.evaluate('window.scrollTo(0,0)')
                    await page.screenshot(path='/tmp/vivideo-profile-history.png',full_page=True)
                    page.once('dialog',lambda dialog:dialog.accept())
                    await page.locator('.watch-delete').first.click()
                    await page.wait_for_function('document.querySelector("#watch-count").textContent==="1"')
                    assert await page.locator('#watch-records .watch-record').count()==1
                    await page.locator('#space-toggle').click()
                    await page.locator('[data-space=roaming]').click()
                    await page.wait_for_function('spaceId==="roaming"&&!spaceTransition')
                    await page.locator('[data-page=profile]').click()
                    await page.wait_for_selector('#watch-records .watch-empty')
                    assert await page.locator('#watch-count').inner_text()=='0'
                    await page.locator('#space-toggle').click()
                    await page.locator('[data-space=learning]').click()
                    await page.wait_for_function('spaceId==="learning"&&!spaceTransition&&document.querySelector("#watch-count").textContent==="1"')
                    await page.set_viewport_size({'width':390,'height':844})
                    await page.evaluate('window.scrollTo(0,0)')
                    await page.screenshot(path='/tmp/vivideo-profile-history-mobile.png',full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert not errors,errors
                    for suffix in ['html','css','js']:
                        assert (await page.request.get(base+'/static/part-c.'+suffix)).status==404
                    await browser.close()
                print('Profile history passed: placement, statistics, interest editing, date presets, search, deletion, real space menu isolation, mobile and retired standalone page.')
            finally:
                process.terminate();process.wait(timeout=10)

if __name__=='__main__':asyncio.run(main())
