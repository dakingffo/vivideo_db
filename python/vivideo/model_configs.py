"""Persistent model connections; secrets never appear in public responses."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from .paths import data_root

router = APIRouter(prefix='/api/model-configs')
Space = Literal['learning', 'roaming']


@contextmanager
def database():
    root = data_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / 'settings.db'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS model_configs (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                model TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS model_selections (
                space_id TEXT PRIMARY KEY, config_id TEXT NOT NULL
            );
        ''')
        with db:
            yield db
    finally:
        db.close()


class ModelConfigInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=120)
    # Omitted/null preserves the existing key; an empty string explicitly removes it.
    api_key: str | None = Field(default=None, max_length=1000)

    @field_validator('name', 'base_url', 'model')
    @classmethod
    def clean(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('字段不能为空')
        return value

    @field_validator('base_url')
    @classmethod
    def valid_url(cls, value):
        try:
            url = urlsplit(value)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.fragment or url.query:
                raise ValueError('请填写不含认证信息、查询参数或片段的 HTTP / HTTPS 地址')
            _ = url.port
        except ValueError:
            raise ValueError('API 地址无效')
        return value.rstrip('/')


class SelectionInput(BaseModel):
    config_id: str | None = None


def public(row):
    return {key: row[key] for key in ('id', 'name', 'base_url', 'model')} | {'has_api_key': bool(row['api_key'])}


@router.get('')
def list_configs(space_id: Space = 'learning'):
    with database() as db:
        selected = db.execute('SELECT config_id FROM model_selections WHERE space_id=?', (space_id,)).fetchone()
        return {'configs': [public(row) for row in db.execute('SELECT * FROM model_configs ORDER BY rowid')],
                'selected_id': selected['config_id'] if selected else None}


@router.post('', status_code=201)
def create_config(config: ModelConfigInput):
    config_id = str(uuid4())
    with database() as db:
        db.execute('INSERT INTO model_configs VALUES (?,?,?,?,?)', (config_id, config.name, config.base_url, config.model, config.api_key or ''))
        return public(db.execute('SELECT * FROM model_configs WHERE id=?', (config_id,)).fetchone())


@router.put('/selection/{space_id}')
def select_config(space_id: Space, selection: SelectionInput):
    with database() as db:
        if selection.config_id is None:
            db.execute('DELETE FROM model_selections WHERE space_id=?', (space_id,))
        else:
            if not db.execute('SELECT 1 FROM model_configs WHERE id=?', (selection.config_id,)).fetchone():
                raise HTTPException(404, '模型配置不存在')
            db.execute('INSERT OR REPLACE INTO model_selections VALUES (?,?)', (space_id, selection.config_id))
    return {'selected_id': selection.config_id}


@router.put('/{config_id}')
def update_config(config_id: str, config: ModelConfigInput):
    with database() as db:
        old = db.execute('SELECT * FROM model_configs WHERE id=?', (config_id,)).fetchone()
        if old is None:
            raise HTTPException(404, '模型配置不存在')
        secret = old['api_key'] if config.api_key is None else config.api_key
        db.execute('UPDATE model_configs SET name=?,base_url=?,model=?,api_key=? WHERE id=?',
                   (config.name, config.base_url, config.model, secret, config_id))
        return public(db.execute('SELECT * FROM model_configs WHERE id=?', (config_id,)).fetchone())


@router.delete('/{config_id}', status_code=204)
def delete_config(config_id: str):
    with database() as db:
        if db.execute('DELETE FROM model_configs WHERE id=?', (config_id,)).rowcount == 0:
            raise HTTPException(404, '模型配置不存在')
        db.execute('DELETE FROM model_selections WHERE config_id=?', (config_id,))
