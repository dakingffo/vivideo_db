"""Optional real browser smoke test. Uses temporary DBs, a local model, and tiny video fixtures.
Run: PYTHONPATH=python:tests python tests/browser_local_video.py
"""
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
from playwright.async_api import async_playwright
from test_local_agent import Model, URL
from vivideo import storage, library, download_worker
from vivideo.model_configs import database as config_database

ROOT=Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


async def main():
    with tempfile.TemporaryDirectory() as directory:
        folder=Path(directory);env={**os.environ,'VIVIDEO_DATA_DIR':str(folder/'data'),'VIVIDEO_LOCAL_VIDEO':'1','VIVIDEO_ZMQ_ENDPOINT':f'tcp://127.0.0.1:{port()}','PYTHONPATH':str(ROOT/'python')}
        with patch.dict(os.environ,env):
            ffmpeg=download_worker.executable('ffmpeg')
            encoders=subprocess.check_output([ffmpeg,'-hide_banner','-encoders']).decode();encoder='libx264' if 'libx264' in encoders else 'libopenh264'
            clip=folder/'clip.mp4'
            subprocess.run([ffmpeg,'-v','error','-nostdin','-f','lavfi','-i','color=c=0x315e50:s=320x180:r=24','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','5','-c:v',encoder,'-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(clip)],check=True,capture_output=True)
            class Fixture(Model):
                def do_GET(self):
                    if self.path=='/clip.mp4':
                        body=clip.read_bytes();self.send_response(200);self.send_header('Content-Type','video/mp4')
                    else:
                        body=b'{"data":[{"id":"model"}]}';self.send_response(200);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            fixture=ThreadingHTTPServer(('127.0.0.1',0),Fixture);thread=threading.Thread(target=fixture.serve_forever,daemon=True);thread.start()
            with config_database() as db:
                db.execute('INSERT INTO model_configs VALUES (?,?,?,?,?)',('fixture','fixture',f'http://127.0.0.1:{fixture.server_port}/v1','model','fixture-secret'))
                db.execute('INSERT INTO model_selections VALUES (?,?)',('learning','fixture'))
            ui_port=port();base=f'http://127.0.0.1:{ui_port}';processes=[]
            try:
                with open(folder/'services.log','w+') as logs:
                    for command in ([sys.executable,'-m','vivideo.worker'],[sys.executable,'-m','uvicorn','vivideo.app:app','--host','127.0.0.1','--port',str(ui_port)]):
                        processes.append(subprocess.Popen(command,cwd=ROOT,env=env,stdout=logs,stderr=logs))
                    async with httpx.AsyncClient(base_url=base,timeout=10) as client:
                        for _ in range(100):
                            try:
                                response=await client.get('/api/health')
                                if response.status_code==200 and response.json()['backend_connected']:break
                            except httpx.HTTPError:pass
                            await asyncio.sleep(.1)
                        else:raise AssertionError('Services failed to start')
                        async with async_playwright() as p:
                            executable=os.getenv('VIVIDEO_BROWSER_BIN')
                            if not executable:
                                executable=next(iter(sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux64/chrome'))),None)
                            browser=await p.chromium.launch(executable_path=str(executable) if executable else None,args=['--no-sandbox','--autoplay-policy=no-user-gesture-required'])
                            page=await browser.new_page(viewport={'width':1440,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                            await page.goto(base);await page.wait_for_function('typeof ready!=="undefined"&&ready')
                            await page.locator('#message').fill('首次想了解机器学习');await page.locator('#send').click();await page.wait_for_function('!busy && activeSession!==null')
                            sid=await page.evaluate('activeSession');await page.wait_for_selector('.preparation-status')
                            assert await page.locator('#conversation .video-card').count()==0
                            composer=await page.locator('.composer-wrap').bounding_box()
                            late=os.getenv('VIVIDEO_BROWSER_LATE')=='1'
                            if not late:
                                search=library.claim_job('learning','browser-search')
                                with patch('vivideo.download_worker.search_videos',return_value=[{'url':URL,'title':'机器学习 Python 实践','summary':'Python 模型训练'}]):await download_worker.run_job('learning',search)
                            await page.locator('#message').fill('Python 动手实践，五分钟');await page.locator('#send').click();await page.wait_for_function('!busy')
                            assert await page.locator('#conversation .video-card').count()==0
                            if late:
                                search=library.claim_job('learning','late-search')
                                with patch('vivideo.download_worker.search_videos',return_value=[{'url':URL,'title':'机器学习 Python 实践','summary':'Python 模型训练'}]):await download_worker.run_job('learning',search)
                            job=library.claim_job('learning','browser-download');assert job['kind']=='download',job
                            real=download_worker.command
                            async def fixture_command(args,*positional,**kwargs):
                                if '--dump-single-json' in args:return json.dumps({'title':'机器学习 Python 实践','duration':5,'uploader':'fixture'}).encode()
                                if '-m' in args and 'yt_dlp' in args:
                                    args=list(args);args[-1]=f'http://127.0.0.1:{fixture.server_port}/clip.mp4';args[args.index('--format')+1]='best'
                                return await real(args,*positional,**kwargs)
                            with patch('vivideo.download_worker.command',side_effect=fixture_command):await download_worker.run_job('learning',job)
                            if late:
                                recommendation=library.claim_job('learning','late-selector');assert recommendation['kind']=='recommend',recommendation
                                await download_worker.run_job('learning',recommendation)
                            await page.wait_for_selector('#conversation .video-card');await page.wait_for_function('document.querySelector("#conversation [data-cover-state=ready]")!==null')
                            assert await page.locator('#conversation .video-card:not([data-loop-copy])').count()==1
                            await page.locator('#message').fill('播放后应保留的草稿')
                            await page.locator('#conversation [data-action=watch]').first.click();await page.wait_for_selector('#watch-mount video')
                            await page.wait_for_function('document.querySelector("#watch-mount video").currentTime>2.5')
                            await page.locator('#watch-mount video').evaluate('(e)=>e.pause()');await page.locator('#close-watch').click()
                            await page.wait_for_timeout(450)
                            assert await page.locator('#message').input_value()=='播放后应保留的草稿'
                            after=await page.locator('.composer-wrap').bounding_box();assert abs(composer['y']-after['y'])<1
                            events=(await client.get('/api/spaces/learning/watch-events')).json()['events'];assert len(events)==1 and events[0]['watched_seconds']>events[0]['duration_seconds']/4,events
                            await page.locator('[data-page=library]').click();await page.wait_for_selector('.cache-row');assert '可播放' in await page.locator('.cache-row').text_content()
                            await page.locator('[data-cache-pin]').click();await page.wait_for_function('document.querySelector("[data-cache-pin]").dataset.pinned==="1"')
                            await page.locator('[data-cache-delete]').click();await page.wait_for_function('document.querySelector("#cache-message").textContent.includes("取消保留")')
                            await page.set_viewport_size({'width':390,'height':844});await page.wait_for_timeout(400)
                            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                            await page.screenshot(path='/tmp/vivideo-local-library-mobile.png',full_page=True)
                            await page.locator('[data-page=discover]').click();await page.wait_for_timeout(600);await page.locator('#history-new').click();await page.wait_for_function('welcomeAnimations.length===0')
                            assert await page.evaluate('preparationEventKey')==''
                            await page.evaluate('switchSpace("roaming")');await page.locator('[data-page=library]').click();await page.wait_for_function('document.querySelector("#local-cache-list").textContent.includes("还没有本地视频")')
                            assert not errors,errors
                            await browser.close()
                            print('Browser passed: real gateway→ZMQ→model→MCP, first clarification, durable prefetch, real yt-dlp/FFmpeg pipeline, SSE selected-card promotion, cover, native playback, 25% watch event, stable input, pin/delete protection, mobile, new-session and space cleanup.')
            finally:
                for process in processes:process.terminate()
                for process in processes:
                    try:process.wait(10)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
                fixture.shutdown();fixture.server_close();thread.join()


if __name__=='__main__':asyncio.run(main())
