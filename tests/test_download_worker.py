"""Exercise actual yt-dlp, FFmpeg and ffprobe using a tiny local HTTP video fixture."""
import asyncio
import functools
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from vivideo.app import app
from vivideo import library,storage,download_worker

URL='https://www.youtube.com/watch?v=dQw4w9WgXcQ'


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


class DownloadPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.base=Path(self.directory.name)
        self.env=patch.dict(os.environ,{'VIVIDEO_DATA_DIR':str(self.base/'data'),'VIVIDEO_LOCAL_VIDEO':'1','VIVIDEO_VIDEO_MAX_BYTES':str(5*1024**2),'VIVIDEO_MEDIA_QUOTA_BYTES':str(100*1024**2)})
        self.env.start();self.addCleanup(self.directory.cleanup);self.addCleanup(self.env.stop)
        try:self.ffmpeg=download_worker.executable('ffmpeg');self.ffprobe=download_worker.executable('ffprobe')
        except download_worker.DownloadError:self.skipTest('FFmpeg/ffprobe not installed')
        try:import yt_dlp  # noqa: F401
        except ImportError:self.skipTest('yt-dlp not installed')
        encoders=subprocess.check_output([self.ffmpeg,'-hide_banner','-encoders']).decode()
        encoder='libx264' if 'libx264' in encoders else 'libopenh264'
        subprocess.run([self.ffmpeg,'-v','error','-nostdin','-f','lavfi','-i','color=c=0x315e50:s=320x180:r=24','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','3','-c:v',encoder,'-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(self.base/'clip.mp4')],check=True,capture_output=True)
        self.http=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(self.base)));self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.thread.join);self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)
        self.vid=library.import_video('learning',URL)['video_id'];self.job=library.claim_job('learning','fixture-worker')

    def test_real_download_merge_cover_validation_and_local_playback(self):
        real=download_worker.command
        async def fixture(args,*positional,**kwargs):
            if '--dump-single-json' in args:
                # Public-platform metadata extraction is isolated; the rest uses real binaries.
                return json.dumps({'title':'机器学习本地播放测试','duration':3,'uploader':'fixture'}).encode()
            if '-m' in args and 'yt_dlp' in args:
                args=list(args);args[-1]=f'http://127.0.0.1:{self.http.server_port}/clip.mp4';args[args.index('--format')+1]='best'
            return await real(args,*positional,**kwargs)
        with patch('vivideo.download_worker.command',side_effect=fixture):asyncio.run(download_worker.run_job('learning',self.job))
        state=library.cache_status('learning')
        self.assertEqual(state['jobs'][0]['status'],'complete',state)
        asset=library.ready_asset('learning',self.vid)
        path=library.asset_path('learning',asset['relative_path']);self.assertTrue(path.is_file())
        self.assertTrue(library.asset_path('learning',asset['thumbnail_path']).is_file())
        self.assertGreater(asset['duration'],2.9);self.assertLess(asset['duration'],3.2)
        media=json.loads(subprocess.check_output([self.ffprobe,'-v','error','-show_streams','-of','json',str(path)]))
        self.assertEqual({s['codec_name'] for s in media['streams']},{'h264','aac'})
        self.assertFalse((library.root('learning')/'staging'/self.job['id']).exists())
        self.assertEqual(state['reserved_bytes'],0)
        client=TestClient(app);playback=client.get(f'/api/spaces/learning/videos/{self.vid}/playback').json()
        response=client.get(playback['stream_url'],headers={'Range':'bytes=0-31'})
        self.assertEqual(response.status_code,206);self.assertEqual(len(response.content),32)
        self.assertIn(b'ftyp',response.content)

    def test_bilibili_adapter_passes_private_info_file_to_real_downloader(self):
        vid=library.import_video('learning','https://www.bilibili.com/video/BV1B7411m7LV')['video_id']
        job=library.claim_job('learning','bili-fixture')
        info={'id':'test-bilibili','title':'B站本地适配测试','duration':3,'extractor_key':'BiliBili','extractor':'bilibili-api',
            'formats':[{'format_id':'progressive','url':f'http://127.0.0.1:{self.http.server_port}/clip.mp4','ext':'mp4','height':180,'vcodec':'avc1','acodec':'mp4a'}]}
        real=download_worker.command;paths=[]
        async def command(args,*positional,**kwargs):
            if '--load-info-json' in args:
                path=Path(args[args.index('--load-info-json')+1]);paths.append(path)
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                self.assertNotIn('--dump-single-json',args)
            return await real(args,*positional,**kwargs)
        with patch('vivideo.bilibili_source.resolve',return_value=info),patch('vivideo.download_worker.command',side_effect=command):
            asyncio.run(download_worker.run_job('learning',job))
        asset=library.ready_asset('learning',vid)
        self.assertTrue(library.asset_path('learning',asset['relative_path']).is_file())
        self.assertTrue(paths);self.assertTrue(all(not path.exists() for path in paths))

    def test_unknown_or_live_duration_never_becomes_ready(self):
        async def bad_metadata(*args,**kwargs):return b'{"title":"live","duration":0,"is_live":true}'
        with patch('vivideo.download_worker.command',side_effect=bad_metadata):asyncio.run(download_worker.run_job('learning',self.job))
        self.assertEqual(library.query_videos('learning'),[])
        self.assertEqual(library.cache_status('learning')['jobs'][0]['status'],'queued')
        self.assertEqual(library.cache_status('learning')['reserved_bytes'],0)
