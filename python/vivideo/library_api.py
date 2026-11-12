"""Local media delivery, cache management and preparation event stream."""
import asyncio
import json
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import library, storage
from .model_configs import Space

router=APIRouter(prefix='/api/spaces/{space_id}')


class ImportVideo(BaseModel):
    url: Annotated[str,Field(min_length=1,max_length=2000)]
    pinned: bool=False


class Pin(BaseModel):
    pinned: bool


class Quota(BaseModel):
    quota_bytes: Annotated[int,Field(ge=1024**2,le=10*1024**4)]


@router.post('/local-videos',status_code=202)
def import_video(space_id: Space, request: ImportVideo):
    return library.import_video(space_id,request.url,request.pinned)


@router.get('/local-videos')
def local_videos(space_id: Space,query: str='',ready_only: bool=True):
    return {'videos':library.query_videos(space_id,query[:300],ready_only)}


@router.get('/cache')
def cache(space_id: Space):
    return library.cache_status(space_id)


@router.put('/cache')
def set_quota(space_id: Space,request: Quota):
    with storage.database(space_id) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR REPLACE INTO media_settings VALUES (1,?)',(request.quota_bytes,))
        try:library.reserve(db,space_id,0)
        except storage.StateError:
            # Transaction rollback leaves the previous quota intact.
            raise storage.StateError('新配额不足以容纳保留、播放中或正在下载的视频')
    return library.cache_status(space_id)


@router.patch('/local-videos/{video_id}')
def pin_video(space_id: Space,video_id: str,request: Pin):
    with storage.database(space_id) as db:
        if not db.execute('UPDATE media_assets SET pinned=? WHERE video_id=?',(int(request.pinned),video_id)).rowcount:
            raise storage.StateError('本地视频不存在',404)
    return {'pinned':request.pinned}


@router.delete('/local-videos/{video_id}',status_code=204)
def delete_video(space_id: Space,video_id: str):
    with storage.database(space_id) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM media_assets WHERE video_id=?',(video_id,)).fetchone()
        if not row:raise storage.StateError('本地视频不存在',404)
        if row['pinned']:raise storage.StateError('请先取消保留，再清理视频')
        if db.execute("SELECT 1 FROM download_jobs WHERE kind='download' AND target=? AND status='running'",(video_id,)).fetchone():
            raise storage.StateError('视频正在下载，请先取消任务')
        db.execute("UPDATE download_jobs SET status='cancelled',reserved=0 WHERE kind='download' AND target=? AND status='queued'",(video_id,))
        library.remove_asset(db,space_id,row)


@router.post('/local-videos/{video_id}/download',status_code=202)
def retry_video(space_id: Space,video_id: str):
    video=storage.get_video(space_id,video_id)
    return library.import_video(space_id,video['url'])


@router.post('/download-jobs/{job_id}/cancel',status_code=204)
def cancel_job(space_id: Space,job_id: UUID):
    with storage.database(space_id) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM download_jobs WHERE id=?',(str(job_id),)).fetchone()
        if not row:raise storage.StateError('任务不存在',404)
        db.execute("UPDATE download_jobs SET status='cancelled' WHERE id=? AND status IN ('queued','running')",(str(job_id),))
        if row['kind']=='download':db.execute("UPDATE media_assets SET state='failed' WHERE video_id=? AND state!='ready'",(row['target'],))


@router.post('/local-videos/{video_id}/lease',status_code=204)
def renew_lease(space_id: Space,video_id: str):
    library.ready_asset(space_id,video_id,touch=True)


@router.api_route('/local-videos/{video_id}/{resource}',methods=['GET','HEAD'])
def resource(space_id: Space,video_id: str,resource: str,request: Request):
    fields={'stream':('relative_path','video/mp4'),'cover':('thumbnail_path','image/webp'),'subtitles':('subtitle_path','text/vtt')}
    if resource not in fields:raise storage.StateError('文件不存在',404)
    field,content_type=fields[resource]
    row=library.ready_asset(space_id,video_id,touch=resource=='stream')
    path=library.asset_path(space_id,row[field])
    headers={'Cache-Control':'private, no-cache','X-Content-Type-Options':'nosniff'}
    return FileResponse(path,media_type=content_type,headers=headers)


@router.get('/sessions/{session_id}/preparation')
def preparation(space_id: Space,session_id: UUID):
    return library.session_preparation(space_id,str(session_id))


@router.get('/sessions/{session_id}/events')
async def events(space_id: Space,session_id: UUID,request: Request):
    storage.get_session(space_id,str(session_id))
    async def stream():
        previous=None;last_sent=0
        while not await request.is_disconnected():
            try:data=await asyncio.to_thread(library.session_preparation,space_id,str(session_id))
            except storage.StateError:
                yield 'event: deleted\ndata: {}\n\n';return
            encoded=json.dumps(data,ensure_ascii=False)
            if encoded!=previous:
                yield 'event: preparation\ndata: '+encoded+'\n\n';previous=encoded;last_sent=time.monotonic()
            elif time.monotonic()-last_sent>15:
                yield ': keepalive\n\n';last_sent=time.monotonic()
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
