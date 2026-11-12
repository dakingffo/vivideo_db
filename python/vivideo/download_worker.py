"""Long-lived, lease-based yt-dlp/FFmpeg worker. No downloads run inside an MCP turn."""
import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from uuid import uuid4

from . import library, storage, bilibili_source
from .paths import bundled_tool
from .search import search_videos

log=logging.getLogger(__name__)


class DownloadError(Exception):
    def __init__(self,message,retryable=True):
        super().__init__(message);self.retryable=retryable


def process_error(stderr,program):
    # Only fixed messages are exposed: never signed URLs, cookie values or raw logs.
    detail=stderr.decode(errors='replace').lower()
    if '412' in detail and ('http error' in detail or 'precondition failed' in detail):
        return DownloadError('来源站点拒绝当前请求（HTTP 412），请稍后重试',False)
    if '429' in detail and ('http error' in detail or 'too many requests' in detail):
        return DownloadError('来源站点请求限流（HTTP 429），请稍后重试',False)
    if 'http error 567' in detail:
        return DownloadError('B站媒体 CDN 拒绝了当前下载请求（HTTP 567），请稍后重试',False)
    if 'requested format is not available' in detail:
        return DownloadError('来源未提供满足清晰度和编码要求的视频格式',False)
    if 'login required' in detail or 'sign in to' in detail:
        return DownloadError('来源要求登录，请配置当前平台的登录信息',False)
    if 'http error 403' in detail:
        return DownloadError('视频访问被拒绝（HTTP 403），请检查来源权限或链接有效期')
    return DownloadError(f'视频处理失败（{program}），请检查来源可用性')


def executable(name):
    configured=os.getenv('VIVIDEO_'+name.upper()+'_BIN')
    found=configured or shutil.which(name) or bundled_tool(name)
    if not found:
        raise DownloadError(f'缺少 {name}，请安装后重试')
    return found


def directory_bytes(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file()) if path.exists() else 0


async def command(args, space, job, staging, timeout=900):
    """Arguments are never interpreted by a shell; terminate the entire subprocess group."""
    process=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
    async def drain(stream, maximum):
        result=bytearray()
        while chunk:=await stream.read(65536):
            if len(result)<maximum:result.extend(chunk[:maximum-len(result)])
        return bytes(result)
    out=asyncio.create_task(drain(process.stdout,2*1024**2));err=asyncio.create_task(drain(process.stderr,65536))
    start=time.monotonic();last_beat=0
    try:
        while process.returncode is None:
            try:await asyncio.wait_for(process.wait(),timeout=.5)
            except TimeoutError:pass
            now=time.monotonic()
            used=directory_bytes(staging)
            if used>library.limits()['max_file']*3:
                raise DownloadError('处理临时文件超过单任务空间预算')
            if shutil.disk_usage(staging).free<64*1024**2:
                raise DownloadError('磁盘剩余空间不足')
            if now-start>timeout:raise DownloadError('视频处理超时')
            if now-last_beat>=5:
                if not library.heartbeat(space,job['id'],job['owner'],min(.95,used/max(1,library.limits()['max_file']))):
                    raise DownloadError('任务已取消或由其他进程恢复')
                last_beat=now
        stdout=await out;stderr=await err
        if process.returncode:
            # Provider output can include signed URLs, cookies, paths; don't expose it.
            raise process_error(stderr,Path(args[0]).name)
        return stdout
    finally:
        if process.returncode is None:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:await asyncio.wait_for(process.wait(),3)
            except TimeoutError:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                await process.wait()
        await asyncio.gather(out,err,return_exceptions=True)


async def search_plan(space, job):
    with storage.database(space) as db:
        plan=db.execute("SELECT p.* FROM preparations p JOIN sessions s ON s.id=p.session_id WHERE p.id=? AND p.status!='superseded' AND s.deleted=0",(job['target'],)).fetchone()
        if not plan:return
        queries=json.loads(plan['queries']);db.execute("UPDATE preparations SET status='searching',error='' WHERE id=?",(plan['id'],))
    # Reuse enough matching local files without waiting for any external search.
    with storage.database(space) as db:
        matches=set()
        for query in queries:
            matches.update(r[0] for r in db.execute("SELECT a.video_id FROM media_assets a JOIN videos v ON v.id=a.video_id WHERE a.state='ready' AND instr(lower(a.title||' '||a.summary||' '||v.query),lower(?))>0 LIMIT 10",(query,)))
        if len(matches)>=library.limits()['batch']:
            current=db.execute("SELECT 1 FROM download_jobs WHERE id=? AND owner=? AND status='running'",(job['id'],job['owner'])).fetchone()
            if current:
                for vid in matches:db.execute('INSERT OR IGNORE INTO plan_candidates VALUES (?,?)',(plan['id'],vid))
                db.execute("UPDATE preparations SET status='prepared' WHERE id=? AND status!='superseded'",(plan['id'],))
                library.schedule_selection(db,plan['id'])
            return
    candidates=[];succeeded=False
    for query in queries:
        if not library.heartbeat(space,job['id'],job['owner']):return
        try:
            results=await search_videos(query,limit=6);succeeded=True
            candidates.extend({**r,'query':query} for r in results)
        except Exception:continue
    if not succeeded:raise DownloadError('网络搜索暂不可用，请稍后重试')
    with storage.database(space) as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT * FROM download_jobs WHERE id=? AND owner=? AND status=\'running\'',(job['id'],job['owner'])).fetchone()
        plan=db.execute("SELECT * FROM preparations WHERE id=? AND status!='superseded'",(job['target'],)).fetchone()
        if not current or not plan:return
        seen=set()
        # Cheap cache hits before any new download; match the current search terms.
        for query in queries:
            for row in db.execute("SELECT a.video_id FROM media_assets a JOIN videos v ON v.id=a.video_id WHERE a.state='ready' AND (instr(lower(a.title||' '||a.summary||' '||v.query),lower(?))>0)",(query,)):
                db.execute('INSERT OR IGNORE INTO plan_candidates VALUES (?,?)',(plan['id'],row['video_id']))
        for row in candidates:
            if len(seen)>=10:break
            vid=library.upsert_video(db,row)
            if vid in seen:continue
            seen.add(vid);db.execute('INSERT OR IGNORE INTO plan_candidates VALUES (?,?)',(plan['id'],vid))
        rows=db.execute("""SELECT a.video_id,a.state FROM plan_candidates c JOIN media_assets a ON a.video_id=c.video_id
            WHERE c.plan_id=? ORDER BY a.state='ready' DESC,c.rowid LIMIT ?""",(plan['id'],library.limits()['batch'])).fetchall()
        for row in rows:
            if row['state']!='ready':library.enqueue(db,'download',row['video_id'],30)
        db.execute("UPDATE preparations SET status='prepared',error=? WHERE id=?",('未找到相关视频' if not candidates and not rows else '',plan['id']))
        library.schedule_selection(db,plan['id'])


async def download_asset(space, job):
    vid=job['target'];base=library.root(space);staging=base/'staging'/job['id']/job['owner'];staging.mkdir(parents=True,exist_ok=True)
    try:
        video=storage.get_video(space,vid)
        # Another worker may have completed this asset before this retry was claimed.
        with storage.database(space) as db:
            row=db.execute('SELECT state FROM media_assets WHERE video_id=?',(vid,)).fetchone()
            if row and row['state']=='ready':return
            db.execute("UPDATE media_assets SET state='downloading' WHERE video_id=?",(vid,))
        ffmpeg=executable('ffmpeg');ffprobe=executable('ffprobe')
        common=[sys.executable,'-m','yt_dlp','--ignore-config','--no-playlist','--no-progress','--socket-timeout','15','--retries','2','--fragment-retries','2','--ffmpeg-location',ffmpeg]
        runtime=os.getenv('VIVIDEO_JS_RUNTIME')
        if not runtime:
            try:runtime='node:'+executable('node')
            except DownloadError:pass
        if runtime:common+=['--js-runtimes',runtime]
        info_path=None
        if '://www.bilibili.com/video/' in video['url']:
            try:info=await bilibili_source.resolve(video['url'],lambda:library.heartbeat(space,job['id'],job['owner']))
            except bilibili_source.SourceError as exc:raise DownloadError(str(exc),exc.retryable) from exc
            info_path=staging/'source.info.json'
            fd=os.open(info_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as file:json.dump(info,file,ensure_ascii=False)
        else:
            raw=await command([*common,'--dump-single-json','--skip-download','--',video['url']],space,job,staging,90)
            try:info=json.loads(raw)
            except ValueError as exc:raise DownloadError('无法读取视频元数据') from exc
        duration=info.get('duration') or 0
        if info.get('is_live') or not isinstance(duration,(int,float)) or not 0<duration<=library.limits()['max_duration']:
            raise DownloadError('视频时长未知、直播中或超过时长限制')
        if (info.get('filesize') or 0)>library.limits()['max_file']:
            raise DownloadError('视频超过单文件大小限制')
        meta={k:info.get(k) for k in ('id','extractor_key','title','description','uploader','duration','webpage_url')}
        with storage.database(space) as db:
            db.execute('UPDATE media_assets SET title=?,summary=?,author=?,duration=?,metadata=? WHERE video_id=?',
                (str(info.get('title') or '')[:500],str(info.get('description') or '')[:4000],str(info.get('uploader') or '')[:200],duration,json.dumps(meta,ensure_ascii=False),vid))
        fmt='bestvideo[height<=720][vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[height<=720][ext=mp4]/bestvideo[height<=720]+bestaudio/best[height<=720]'
        await command([*common,'--format',fmt,'--max-filesize',str(library.limits()['max_file']),
            '--merge-output-format','mp4','--write-subs','--sub-langs','zh-Hans,zh-CN,zh,en','--sub-format','vtt',
            '--paths',str(staging),'--output','source.%(ext)s',
            *(['--load-info-json',str(info_path)] if info_path else ['--',video['url']])],space,job,staging)
        sources=[p for p in staging.glob('source.*') if p.suffix in ('.mp4','.mkv','.webm','.mov')]
        if len(sources)!=1:raise DownloadError('未取得完整视频文件')
        source=sources[0]
        raw=await command([ffprobe,'-v','error','-show_format','-show_streams','-of','json',str(source)],space,job,staging,30)
        probe=json.loads(raw);streams=probe.get('streams',[])
        v=next((s for s in streams if s.get('codec_type')=='video'),None)
        a=next((s for s in streams if s.get('codec_type')=='audio'),None)
        if not v:raise DownloadError('文件没有有效视频流')
        actual=float(probe.get('format',{}).get('duration',0))
        if not 0<actual<=library.limits()['max_duration'] or actual<duration*.95:
            raise DownloadError('视频不完整或时长超过限制')
        with storage.database(space) as db:db.execute("UPDATE media_assets SET state='processing' WHERE video_id=?",(vid,))
        output=staging/'video.mp4'
        args=[ffmpeg,'-nostdin','-v','error','-y','-i',str(source),'-map','0:v:0','-map','0:a:0?']
        if v.get('codec_name')=='h264':args+=['-c:v','copy']
        else:
            encoders=(await command([ffmpeg,'-hide_banner','-encoders'],space,job,staging,15)).decode(errors='replace')
            if 'libx264' in encoders:args+=['-c:v','libx264','-preset','veryfast','-crf','23','-pix_fmt','yuv420p']
            elif 'libopenh264' in encoders:args+=['-c:v','libopenh264','-b:v','2M','-pix_fmt','yuv420p']
            else:raise DownloadError('FFmpeg 缺少可用的 H.264 编码器')
        args+=['-c:a','copy' if a and a.get('codec_name')=='aac' else 'aac','-movflags','+faststart',str(output)]
        await command(args,space,job,staging)
        if not output.is_file() or output.stat().st_size>library.limits()['max_file']:
            raise DownloadError('合并后视频超过单文件大小限制')
        cover=staging/'cover.webp'
        await command([ffmpeg,'-nostdin','-v','error','-y','-i',str(output),'-frames:v','1','-vf','scale=640:-2',str(cover)],space,job,staging,30)
        source.unlink()
        # Keep only publishable artifacts; final disk accounting includes cover/subtitles.
        for path in staging.iterdir():
            if path.name not in ('video.mp4','cover.webp') and path.suffix!='.vtt':path.unlink(missing_ok=True)
        size=directory_bytes(staging)
        if size>library.limits()['max_file']:raise DownloadError('视频及附件超过单文件大小限制')
        if not library.heartbeat(space,job['id'],job['owner']):raise DownloadError('任务已取消')
        # Unique attempt directory avoids an old lease owner overwriting a newer result.
        final=base/'assets'/(vid+'-'+job['owner']);final.parent.mkdir(parents=True,exist_ok=True)
        os.replace(staging,final)
        with storage.database(space) as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute("SELECT 1 FROM download_jobs WHERE id=? AND owner=? AND status='running'",(job['id'],job['owner'])).fetchone()
            if not current:
                shutil.rmtree(final,ignore_errors=True);return
            subtitles=next(final.glob('*.vtt'),None)
            relative=lambda p:str(p.relative_to(base)) if p else None
            db.execute("UPDATE media_assets SET state='ready',relative_path=?,thumbnail_path=?,subtitle_path=?,duration=?,bytes=?,last_access=? WHERE video_id=?",
                (relative(final/'video.mp4'),relative(final/'cover.webp'),relative(subtitles),actual,size,time.time(),vid))
            for row in db.execute("SELECT DISTINCT p.session_id FROM preparations p JOIN plan_candidates c ON c.plan_id=p.id JOIN sessions s ON s.id=p.session_id WHERE c.video_id=? AND p.status!='superseded' AND s.deleted=0",(vid,)).fetchall():
                library.publish(db,space,row['session_id'])
                current=library.latest_plan(db,row['session_id'])
                if current:library.schedule_selection(db,current['id'])
    finally:
        # Retry starts from a clean attempt; expired lease files are cleaned by reconcile.
        shutil.rmtree(staging,ignore_errors=True)
        try:staging.parent.rmdir()
        except OSError:pass


async def run_job(space, job):
    try:
        if job['kind']=='search':await search_plan(space,job)
        elif job['kind']=='recommend':
            from .agent import select_cached_videos
            # Renew ownership while an external model is responding.
            async def renew():
                while True:
                    await asyncio.sleep(10)
                    if not library.heartbeat(space,job['id'],job['owner']):return
            renewal=asyncio.create_task(renew())
            try:
                async with asyncio.timeout(70):await select_cached_videos(space,job['target'])
            finally:
                renewal.cancel();await asyncio.gather(renewal,return_exceptions=True)
        else:await download_asset(space,job)
        library.finish_job(space,job)
    except asyncio.CancelledError:
        library.finish_job(space,job,'服务停止，任务等待恢复');raise
    except Exception as exc:
        message=str(exc) if isinstance(exc,DownloadError) else '视频准备失败，请重试或更换来源'
        library.finish_job(space,job,message,retry=getattr(exc,'retryable',True))


async def serve():
    for space in storage.SPACES:library.reconcile(space)
    last_reconcile=time.monotonic()
    tasks=set();platforms=set();concurrency=max(1,min(8,int(os.getenv('VIVIDEO_DOWNLOAD_CONCURRENCY','2'))));rotation=0
    try:
        while True:
            if time.monotonic()-last_reconcile>60:
                for space in storage.SPACES:library.reconcile(space)
                last_reconcile=time.monotonic()
            if len(tasks)<concurrency:
                spaces=list(storage.SPACES);spaces=spaces[rotation:]+spaces[:rotation];rotation=(rotation+1)%len(spaces)
                for space in spaces:
                    if len(tasks)>=concurrency:break
                    owner=uuid4().hex;job=library.claim_job(space,owner)
                    if not job:continue
                    platform=job['kind']
                    if job['kind']=='download':
                        url=storage.get_video(space,job['target'])['url']
                        platform='bilibili' if 'bilibili.com' in url else ('youtube' if 'youtube.com' in url else 'vimeo')
                    if platform in platforms:
                        library.finish_job(space,job,'等待同平台任务完成',retry=True)
                        # Waiting for a platform slot is not a failed attempt.
                        with storage.database(space) as db:db.execute("UPDATE download_jobs SET status='queued',attempts=max(0,attempts-1),not_before=? WHERE id=?",(time.time()+2,job['id']))
                        continue
                    platforms.add(platform)
                    async def execute(s=space,j=job,p=platform):
                        try:await run_job(s,j)
                        finally:platforms.discard(p)
                    task=asyncio.create_task(execute());tasks.add(task);task.add_done_callback(tasks.discard)
            await asyncio.sleep(.5)
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def main():
    task=asyncio.create_task(serve());loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,task.cancel)
    try:await task
    except asyncio.CancelledError:pass


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    if '--check-tools' in sys.argv:
        import yt_dlp.version
        print(json.dumps({'yt_dlp':yt_dlp.version.__version__,'ffmpeg':executable('ffmpeg'),
                          'ffprobe':executable('ffprobe'),'node':os.getenv('VIVIDEO_JS_RUNTIME') or executable('node')},ensure_ascii=False))
    else:asyncio.run(main())
