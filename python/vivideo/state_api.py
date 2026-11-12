"""UI endpoints for server-owned conversations and explicitly confirmed space traits."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field, StringConstraints

from . import storage, media, library
from .model_configs import Space

router=APIRouter(prefix='/api/spaces/{space_id}')
Text=Annotated[str,StringConstraints(strip_whitespace=True,min_length=1,max_length=200)]


class NewSession(BaseModel):
    title: Annotated[str,StringConstraints(strip_whitespace=True,min_length=1,max_length=80)]='新对话'


class TraitInput(BaseModel):
    text: Text


class ProposalDecision(BaseModel):
    accept: bool
    text: Text | None = None


@router.get('/sessions')
def sessions(space_id: Space):
    return {'sessions':storage.list_sessions(space_id)}


@router.post('/sessions',status_code=201)
def create_session(space_id: Space, request: NewSession):
    return storage.create_session(space_id,request.title)


@router.get('/sessions/{session_id}')
def session(space_id: Space,session_id: UUID):
    return storage.get_session(space_id,str(session_id))


@router.delete('/sessions/{session_id}',status_code=204)
def delete_session(space_id: Space,session_id: UUID):
    storage.delete_session(space_id,str(session_id))


@router.post('/requests/{request_id}/cancel',status_code=204)
def cancel_request(space_id: Space,request_id: UUID):
    storage.cancel_request(space_id,str(request_id))


@router.get('/traits')
def traits(space_id: Space):
    return {'traits':storage.list_traits(space_id)}


@router.post('/traits',status_code=201)
def add_trait(space_id: Space,request: TraitInput):
    return {'traits':storage.add_trait(space_id,request.text)}


@router.delete('/traits/{trait_id}',status_code=204)
def delete_trait(space_id: Space,trait_id: UUID):
    storage.delete_trait(space_id,str(trait_id))


@router.post('/trait-proposals/{proposal_id}/decision')
def decide_proposal(space_id: Space,proposal_id: UUID,request: ProposalDecision):
    return {'traits':storage.decide_proposal(space_id,str(proposal_id),request.accept,request.text)}


@router.get('/videos')
def videos(space_id: Space,query: Annotated[str,Field(max_length=300)]=''):
    return {'videos':storage.list_videos(space_id,query)}


class WatchInput(BaseModel):
    event_id: UUID
    video_id: Annotated[str,Field(min_length=1,max_length=100)]
    session_id: UUID | None = None
    watched_seconds: Annotated[float,Field(gt=0,le=604800,allow_inf_nan=False)]
    duration_seconds: Annotated[float,Field(gt=0,le=604800,allow_inf_nan=False)]


@router.get('/videos/{video_id}/metadata')
async def video_metadata(space_id: Space,video_id: str):
    video=storage.get_video(space_id,video_id)
    if library.enabled():
        asset=library.ready_asset(space_id,video_id)
        return {'thumbnail_url':f'/api/spaces/{space_id}/local-videos/{video_id}/cover','duration_seconds':asset['duration'],'provider':'local'}
    return media.public_metadata(await media.metadata(video['url']))


@router.get('/videos/{video_id}/playback')
async def video_playback(space_id: Space,video_id: str):
    video=storage.get_video(space_id,video_id)
    if library.enabled():
        asset=library.ready_asset(space_id,video_id,touch=True)
        return {'kind':'native','provider':'local','tracking_supported':True,'duration_seconds':asset['duration'],
                'stream_url':f'/api/spaces/{space_id}/local-videos/{video_id}/stream',
                'thumbnail_url':f'/api/spaces/{space_id}/local-videos/{video_id}/cover',
                'subtitles_url':f'/api/spaces/{space_id}/local-videos/{video_id}/subtitles' if asset['subtitle_path'] else None}
    return await media.playback(video['url'])


@router.post('/watch-events')
def record_watch(space_id: Space,request: WatchInput):
    return storage.record_watch(space_id,str(request.event_id),request.video_id,
        str(request.session_id) if request.session_id else None,request.watched_seconds,request.duration_seconds)


@router.get('/watch-events')
def watch_events(space_id: Space):
    return {'events':storage.list_watch_events(space_id)}
