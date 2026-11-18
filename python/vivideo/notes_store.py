"""Space-local video notes and atomic, optimistic-versioned snapshots."""
import time
from uuid import uuid4

from . import storage
from .storage import StateError


def ensure_schema(db):
    """Add only note tables; execute individually to preserve caller transactions."""
    for statement in (
        '''CREATE TABLE IF NOT EXISTS video_notes (
            id TEXT PRIMARY KEY,
            video_id TEXT NOT NULL REFERENCES videos(id),
            title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 80),
            content TEXT NOT NULL CHECK(length(content) <= 10000),
            version INTEGER NOT NULL CHECK(version >= 1),
            created INTEGER NOT NULL, updated INTEGER NOT NULL
        )''',
        '''CREATE TABLE IF NOT EXISTS note_revisions (
            note_id TEXT NOT NULL REFERENCES video_notes(id) ON DELETE CASCADE,
            version INTEGER NOT NULL CHECK(version >= 1),
            title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 80),
            content TEXT NOT NULL CHECK(length(content) <= 10000),
            saved INTEGER NOT NULL, PRIMARY KEY(note_id, version)
        )''',
        'CREATE INDEX IF NOT EXISTS idx_video_notes_updated ON video_notes(updated DESC, id)',
        '''CREATE INDEX IF NOT EXISTS idx_video_notes_video_updated
            ON video_notes(video_id, updated DESC, id)''',
    ):
        db.execute(statement)


FROM_NOTES = '''FROM video_notes n JOIN videos v ON v.id = n.video_id
    LEFT JOIN media_assets a ON a.video_id = v.id'''
FIELDS = """n.id, n.video_id, COALESCE(NULLIF(a.title, ''), v.id) AS video_title,
    n.title, n.version, n.created, n.updated"""


def _get(db, note_id):
    row = db.execute(f'SELECT {FIELDS}, n.content {FROM_NOTES} WHERE n.id = ?',
                     (note_id,)).fetchone()
    if row is None:
        raise StateError('笔记不存在', 404)
    return dict(row)


def _save_revision(db, note):
    db.execute('''INSERT INTO note_revisions(note_id, version, title, content, saved)
        VALUES (?, ?, ?, ?, ?)''',
        (note['id'], note['version'], note['title'], note['content'], note['updated']))


def _page(items, total, limit, offset):
    return dict(items=items, total=total, limit=limit, offset=offset)


def create_note(space, video_id, title, content):
    with storage.database(space) as db:
        ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM videos WHERE id = ?', (video_id,)).fetchone() is None:
            raise StateError('视频不在当前空间中', 404)
        note_id, now = str(uuid4()), int(time.time() * 1000)
        db.execute('''INSERT INTO video_notes
            (id, video_id, title, content, version, created, updated)
            VALUES (?, ?, ?, ?, 1, ?, ?)''', (note_id, video_id, title, content, now, now))
        note = _get(db, note_id)
        _save_revision(db, note)
        return note


def get_note(space, note_id):
    with storage.database(space) as db:
        ensure_schema(db)
        return _get(db, note_id)


def list_notes(space, video_id=None, q='', limit=20, offset=0):
    clauses, params = [], []
    if video_id is not None:
        clauses.append('n.video_id = ?')
        params.append(video_id)
    q = q.strip()
    if q:
        clauses.append('(instr(n.title, ?) > 0 OR instr(n.content, ?) > 0)')
        params.extend((q, q))
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with storage.database(space) as db:
        ensure_schema(db)
        # Count and page share one read snapshot even if another client saves.
        db.execute('BEGIN')
        total = db.execute(f'SELECT count(*) {FROM_NOTES}{where}', params).fetchone()[0]
        rows = db.execute(f'''SELECT {FIELDS}, substr(n.content, 1, 80) AS excerpt
            {FROM_NOTES}{where} ORDER BY n.updated DESC, n.id ASC LIMIT ? OFFSET ?''',
            (*params, limit, offset))
        return _page([dict(row) for row in rows], total, limit, offset)


def update_note(space, note_id, title, content, expected_version):
    with storage.database(space) as db:
        ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        changed = db.execute('''UPDATE video_notes
            SET title = ?, content = ?, version = version + 1, updated = ?
            WHERE id = ? AND version = ?''',
            (title, content, int(time.time() * 1000), note_id, expected_version))
        if changed.rowcount == 0:
            _get(db, note_id)  # Distinguish absent note (404) from stale version (409).
            raise StateError('笔记已被其他页面修改，请复制当前内容后重新加载', 409)
        note = _get(db, note_id)
        _save_revision(db, note)
        return note


def delete_note(space, note_id):
    with storage.database(space) as db:
        ensure_schema(db)
        db.execute('BEGIN IMMEDIATE')
        if db.execute('DELETE FROM video_notes WHERE id = ?', (note_id,)).rowcount == 0:
            raise StateError('笔记不存在', 404)


def list_revisions(space, note_id, limit=20, offset=0):
    with storage.database(space) as db:
        ensure_schema(db)
        db.execute('BEGIN')
        _get(db, note_id)
        total = db.execute('SELECT count(*) FROM note_revisions WHERE note_id = ?',
                           (note_id,)).fetchone()[0]
        rows = db.execute('''SELECT note_id, version, title, content, saved
            FROM note_revisions WHERE note_id = ? ORDER BY version DESC LIMIT ? OFFSET ?''',
            (note_id, limit, offset))
        return _page([dict(row) for row in rows], total, limit, offset)
