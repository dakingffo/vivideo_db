"""Validated HTTP contract for video notes; no model/download dependencies."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import notes_store
from .model_configs import Space

router = APIRouter(prefix='/api/spaces/{space_id}', tags=['notes'])
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Content = Annotated[str, StringConstraints(max_length=10000)]
VideoID = Annotated[str, StringConstraints(min_length=1, max_length=100)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
Search = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]


class NoteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: Title
    content: Content


class NewNote(NoteInput):
    video_id: VideoID


class EditNote(NoteInput):
    expected_version: Annotated[int, Field(ge=1, strict=True)]


@router.get('/notes')
def notes(space_id: Space, video_id: VideoID | None = None,
          q: Search = '', limit: Limit = 20, offset: Offset = 0):
    return notes_store.list_notes(space_id, video_id, q, limit, offset)


@router.post('/notes', status_code=201)
def create_note(space_id: Space, request: NewNote):
    return notes_store.create_note(space_id, request.video_id, request.title, request.content)


@router.get('/notes/{note_id}')
def note(space_id: Space, note_id: UUID):
    return notes_store.get_note(space_id, str(note_id))


@router.put('/notes/{note_id}')
def update_note(space_id: Space, note_id: UUID, request: EditNote):
    return notes_store.update_note(space_id, str(note_id), request.title,
                                   request.content, request.expected_version)


@router.delete('/notes/{note_id}', status_code=204)
def delete_note(space_id: Space, note_id: UUID):
    notes_store.delete_note(space_id, str(note_id))
    return Response(status_code=204)


@router.get('/notes/{note_id}/revisions')
def revisions(space_id: Space, note_id: UUID, limit: Limit = 20, offset: Offset = 0):
    return notes_store.list_revisions(space_id, str(note_id), limit, offset)
