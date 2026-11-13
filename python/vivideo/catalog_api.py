"""HTTP API for the video catalog and its content tags."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from . import catalog_store
from .model_configs import Space
from .storage import StateError


router = APIRouter(prefix='/api/spaces/{space_id}')
CacheState = Literal['missing', 'queued', 'downloading', 'processing', 'ready', 'failed']


def _strip(value):
    return value.strip() if isinstance(value, str) else value


class TagName(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, BeforeValidator(_strip), Field(min_length=1, max_length=30)]


class VideoTags(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tag_ids: list[UUID] = Field(max_length=20)


@router.get('/video-tags')
def tags(space_id: Space):
    return catalog_store.list_tags(space_id)


@router.post('/video-tags', status_code=201)
def create_tag(space_id: Space, request: TagName):
    return catalog_store.create_tag(space_id, request.name)


@router.patch('/video-tags/{tag_id}')
def rename_tag(space_id: Space, tag_id: UUID, request: TagName):
    return catalog_store.rename_tag(space_id, str(tag_id), request.name)


@router.delete('/video-tags/{tag_id}', status_code=204)
def delete_tag(space_id: Space, tag_id: UUID):
    catalog_store.delete_tag(space_id, str(tag_id))


@router.get('/video-catalog')
def catalog(
    space_id: Space,
    q: Annotated[str, BeforeValidator(_strip), Query(max_length=100)] = '',
    tag_ids: Annotated[list[UUID] | None, Query(max_length=20)] = None,
    tag_mode: Literal['all', 'any'] = 'all',
    untagged: bool = False,
    cache_state: CacheState | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    if untagged and tag_ids:
        raise StateError('只看未分类与标签筛选不能同时使用', 422)
    return catalog_store.query_catalog(
        space_id, q=q, tag_ids=[str(tag_id) for tag_id in tag_ids or ()],
        tag_mode=tag_mode, untagged=untagged, cache_state=cache_state,
        limit=limit, offset=offset,
    )


@router.put('/video-catalog/{video_id}/tags')
def replace_video_tags(
    space_id: Space,
    video_id: Annotated[str, Path(min_length=1, max_length=100)],
    request: VideoTags,
):
    return catalog_store.replace_video_tags(
        space_id, video_id, [str(tag_id) for tag_id in request.tag_ids],
    )
