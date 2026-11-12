"""Independent database and provider connectivity checks, without generating tokens."""
import asyncio
import hashlib
import json
import time

import httpx
from fastapi import APIRouter

from . import storage
from .model_configs import Space, database as settings_database

router = APIRouter()
_cache = {}


def read_state(space):
    database_status = {'state': 'connected'}
    config = None
    try:
        with settings_database() as db:
            row = db.execute('''SELECT c.* FROM model_configs c JOIN model_selections s
                ON c.id=s.config_id WHERE s.space_id=?''', (space,)).fetchone()
            config = dict(row) if row else None
        with storage.database(space) as db:
            db.execute('SELECT COUNT(*) FROM sessions').fetchone()
            db.execute('SELECT COUNT(*) FROM videos').fetchone()
    except Exception:
        database_status = {'state': 'disconnected'}
    return database_status, config


def fingerprint(config):
    return hashlib.sha256(json.dumps([str(storage.data_root()), config], sort_keys=True).encode()).hexdigest()



async def probe_model(config):
    if not config:
        return {'state': 'unconfigured'}
    key = fingerprint(config)
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    headers = {'Authorization': 'Bearer ' + config['api_key']} if config['api_key'] else {}
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.get(config['base_url'].rstrip('/') + '/models', headers=headers)
        if response.status_code in (401, 403):
            result = {'state': 'unauthorized'}
        elif response.status_code in (404, 405):
            result = {'state': 'unverified'}
        elif response.status_code == 429:
            result = {'state': 'limited'}
        elif response.status_code != 200:
            result = {'state': 'disconnected'}
        else:
            data = response.json()
            models = data.get('data') if isinstance(data, dict) else None
            if not isinstance(models, list) or any(not isinstance(m, dict) or not isinstance(m.get('id'), str) for m in models):
                result = {'state': 'unverified'}
            elif not any(m['id'] == config['model'] for m in models):
                result = {'state': 'model_unlisted'}
            else:
                result = {'state': 'connected', 'verified_by': 'models'}
    except httpx.HTTPError:
        result = {'state': 'disconnected'}
    except (ValueError, TypeError):
        result = {'state': 'unverified'}
    if len(_cache) >= 256:
        _cache.clear()
    _cache[key] = (time.monotonic() + 60, result)
    return result


@router.get('/api/service-status')
async def service_status(space_id: Space = 'learning'):
    db, config = await asyncio.to_thread(read_state, space_id)
    return {'database': db, 'model_api': await probe_model(config)}
