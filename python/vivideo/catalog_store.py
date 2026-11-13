"""Space-local tags and the video catalog, backed by the existing SQLite database."""
import sqlite3
from time import time_ns
from uuid import uuid4

from .storage import StateError, database


_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS catalog_tags (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 30),
        created INTEGER NOT NULL,
        updated INTEGER NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS catalog_video_tags (
        video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        tag_id TEXT NOT NULL REFERENCES catalog_tags(id) ON DELETE CASCADE,
        PRIMARY KEY(video_id, tag_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_catalog_video_tags_tag
       ON catalog_video_tags(tag_id, video_id)''',
    '''CREATE INDEX IF NOT EXISTS idx_catalog_videos_created
       ON videos(created DESC, id)''',
)

_VIDEO_SOURCE = 'FROM videos v LEFT JOIN media_assets a ON a.video_id = v.id'
_TITLE = "COALESCE(NULLIF(a.title, ''), v.id)"
_AUTHOR = "COALESCE(a.author, '')"
_CACHE_STATE = "COALESCE(a.state, 'missing')"
_VISIBLE = f"{_CACHE_STATE} <> 'evicted'"
_TAG_COUNTS = f'''SELECT t.id, t.name, t.created, t.updated,
                        COALESCE(c.video_count, 0) AS video_count
                 FROM catalog_tags t
                 LEFT JOIN (
                     SELECT vt.tag_id, COUNT(*) AS video_count
                     FROM catalog_video_tags vt
                     LEFT JOIN media_assets a ON a.video_id = vt.video_id
                     WHERE {_VISIBLE}
                     GROUP BY vt.tag_id
                 ) c ON c.tag_id = t.id'''


def ensure_schema(db):
    for statement in _SCHEMA:
        db.execute(statement)


def list_tags(space):
    with database(space) as db:
        ensure_schema(db)
        db.execute('BEGIN')
        items = [dict(row) for row in db.execute(f'{_TAG_COUNTS} ORDER BY t.name, t.id')]
        return {'items': items, 'total': len(items)}


def create_tag(space, name):
    tag_id = str(uuid4())
    now = time_ns() // 1_000_000
    with database(space) as db:
        ensure_schema(db)
        try:
            db.execute(
                'INSERT INTO catalog_tags(id, name, created, updated) VALUES (?, ?, ?, ?)',
                (tag_id, name, now, now),
            )
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                raise StateError('标签名称已存在', 409) from exc
            raise
    return {'id': tag_id, 'name': name, 'created': now, 'updated': now, 'video_count': 0}


def rename_tag(space, tag_id, name):
    with database(space) as db:
        ensure_schema(db)
        try:
            updated = db.execute(
                'UPDATE catalog_tags SET name = ?, updated = ? WHERE id = ?',
                (name, time_ns() // 1_000_000, tag_id),
            )
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                raise StateError('标签名称已存在', 409) from exc
            raise
        if updated.rowcount == 0:
            raise StateError('标签不在当前空间中', 404)
        return dict(db.execute(f'{_TAG_COUNTS} WHERE t.id = ?', (tag_id,)).fetchone())


def delete_tag(space, tag_id):
    with database(space) as db:
        ensure_schema(db)
        deleted = db.execute('DELETE FROM catalog_tags WHERE id = ?', (tag_id,))
        if deleted.rowcount == 0:
            raise StateError('标签不在当前空间中', 404)


def _placeholders(values):
    return ', '.join('?' for _ in values)


def _require_tags(db, tag_ids):
    if not tag_ids:
        return
    count = db.execute(
        f'SELECT COUNT(*) FROM catalog_tags WHERE id IN ({_placeholders(tag_ids)})',
        tag_ids,
    ).fetchone()[0]
    if count != len(tag_ids):
        raise StateError('至少一个标签不在当前空间中', 404)


def _tags_for_videos(db, video_ids):
    tags = {video_id: [] for video_id in video_ids}
    if not tags:
        return tags
    rows = db.execute(
        f'''SELECT vt.video_id, t.id, t.name
            FROM catalog_video_tags vt JOIN catalog_tags t ON t.id = vt.tag_id
            WHERE vt.video_id IN ({_placeholders(video_ids)})
            ORDER BY t.name, t.id''',
        video_ids,
    )
    for row in rows:
        tags[row['video_id']].append({'id': row['id'], 'name': row['name']})
    return tags


def replace_video_tags(space, video_id, tag_ids):
    tag_ids = list(dict.fromkeys(tag_ids))
    with database(space) as db:
        ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        if not db.execute(
            f'SELECT 1 {_VIDEO_SOURCE} WHERE v.id = ? AND {_VISIBLE}', (video_id,)
        ).fetchone():
            raise StateError('视频不在当前空间中或缓存已清理', 404)
        _require_tags(db, tag_ids)
        db.execute('DELETE FROM catalog_video_tags WHERE video_id = ?', (video_id,))
        if tag_ids:
            db.executemany(
                'INSERT INTO catalog_video_tags(video_id, tag_id) VALUES (?, ?)',
                [(video_id, tag_id) for tag_id in tag_ids],
            )
        return {'video_id': video_id, 'tags': _tags_for_videos(db, [video_id])[video_id]}


def _catalog_filter(q, tag_ids, tag_mode, untagged, cache_state):
    clauses = [_VISIBLE]
    args = []
    if q:
        clauses.append(f'(instr({_TITLE}, ?) > 0 OR instr({_AUTHOR}, ?) > 0)')
        args.extend((q, q))
    if cache_state is not None:
        clauses.append(f'{_CACHE_STATE} = ?')
        args.append(cache_state)
    if untagged:
        clauses.append('NOT EXISTS (SELECT 1 FROM catalog_video_tags vt WHERE vt.video_id = v.id)')
    if tag_ids:
        matched = f'SELECT video_id FROM catalog_video_tags WHERE tag_id IN ({_placeholders(tag_ids)})'
        args.extend(tag_ids)
        if tag_mode == 'all':
            matched += ' GROUP BY video_id HAVING COUNT(DISTINCT tag_id) = ?'
            args.append(len(tag_ids))
        clauses.append(f'v.id IN ({matched})')
    return ' AND '.join(clauses), args


def query_catalog(space, *, q='', tag_ids=(), tag_mode='all', untagged=False,
                  cache_state=None, limit=20, offset=0):
    tag_ids = list(dict.fromkeys(tag_ids))
    where, args = _catalog_filter(q, tag_ids, tag_mode, untagged, cache_state)
    with database(space) as db:
        ensure_schema(db)
        db.execute('BEGIN')
        _require_tags(db, tag_ids)
        total = db.execute(f'SELECT COUNT(*) {_VIDEO_SOURCE} WHERE {where}', args).fetchone()[0]
        if offset >= total:
            return {'items': [], 'total': total, 'limit': limit, 'offset': offset}
        rows = db.execute(
            f'''SELECT v.id AS video_id, v.url, v.created, {_TITLE} AS title,
                       {_AUTHOR} AS author, CASE WHEN a.duration > 0 THEN a.duration END AS duration_seconds,
                       {_CACHE_STATE} AS cache_state
                {_VIDEO_SOURCE} WHERE {where}
                ORDER BY v.created DESC, v.id ASC LIMIT ? OFFSET ?''',
            [*args, limit, offset],
        ).fetchall()
        tags = _tags_for_videos(db, [row['video_id'] for row in rows])
        items = [dict(row, tags=tags[row['video_id']]) for row in rows]
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}
