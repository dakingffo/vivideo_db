"""Space-scoped durable state. SQLite is authoritative; Markdown is a projection."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from .library_schema import SCHEMA
from .paths import data_root

SPACES = ('learning', 'roaming')


class StateError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def space_root(space):
    if space not in SPACES:
        raise StateError('空间不存在', 404)
    return data_root() / 'spaces' / space


@contextmanager
def database(space):
    root = space_root(space)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / 'space.db'
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, updated INTEGER NOT NULL,
        memory TEXT NOT NULL DEFAULT '', memory_version INTEGER NOT NULL DEFAULT 0,
        deleted INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        role TEXT NOT NULL, content TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}'
      );
      CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        message TEXT NOT NULL, personalized INTEGER NOT NULL, status TEXT NOT NULL,
        result TEXT, started REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS cancelled_requests (id TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS videos (
        id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, query TEXT NOT NULL, created INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS watch_events (
        id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
        session_id TEXT REFERENCES sessions(id), watched_seconds REAL NOT NULL,
        duration_seconds REAL NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS trait_proposals (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        request_id TEXT NOT NULL REFERENCES requests(id), text TEXT NOT NULL, evidence TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
      );
      CREATE TABLE IF NOT EXISTS space_traits (
        id TEXT PRIMARY KEY, text TEXT UNIQUE NOT NULL, source_proposal TEXT, created INTEGER NOT NULL
      );
    ''')
    db.executescript(SCHEMA)
    try:
        with db:
            yield db
    finally:
        db.close()


def require_session(db, sid):
    row = db.execute('SELECT * FROM sessions WHERE id=? AND deleted=0', (sid,)).fetchone()
    if row is None:
        raise StateError('会话不存在或已删除', 404)
    return row


def create_session(space, title='新对话'):
    sid = str(uuid4())
    with database(space) as db:
        db.execute('INSERT INTO sessions(id,title,updated) VALUES (?,?,?)', (sid, title, int(time.time()*1000)))
    export_memories(space, sid)
    return get_session(space, sid)


def snapshot(db, row, space):
    from . import library
    history, turns, proposals = [], [], []
    for msg in db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY id', (row['id'],)):
        history.append({'role': msg['role'], 'content': msg['content']})
        if msg['role'] == 'assistant':
            payload = json.loads(msg['payload'])
            turns.append({'messageIndex': len(history)-1, 'videos': library.visible_videos(db,space,payload.get('videos', [])), 'demo': False, 'requestId':payload.get('request_id')})
    for proposal in db.execute('SELECT * FROM trait_proposals WHERE session_id=? ORDER BY rowid', (row['id'],)):
        proposals.append(dict(proposal))
    return dict(id=row['id'], title=row['title'], updated=row['updated'], history=history,
                recommendationTurns=turns, videos=turns[-1]['videos'] if turns else [], demo=False,
                memory=row['memory'], memory_version=row['memory_version'], trait_proposals=proposals)


def list_sessions(space):
    with database(space) as db:
        return [snapshot(db, row, space) for row in db.execute('SELECT * FROM sessions WHERE deleted=0 ORDER BY updated DESC')]


def get_session(space, sid):
    with database(space) as db:
        return snapshot(db, require_session(db, sid), space)


def model_history(space,sid,config_id,max_chars=24000):
    """Replay whole protocol turns, never leave unmatched tool calls after trimming."""
    with database(space) as db:
        require_session(db,sid)
        rows=list(reversed(list(db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 30',(sid,)))))
    groups=[]
    for index in range(0,len(rows)-1,2):
        user,assistant=rows[index:index+2]
        payload=json.loads(assistant['payload'])
        groups.append((payload.get('_config_id'),[{'role':'user','content':user['content']},*payload.get('_protocol_turn',[])]))
    kept=[]
    size=0
    for source,group in reversed(groups):
        # Different providers/models may use incompatible protocol extensions.
        if source!=config_id or len(group)<2:
            break
        length=len(json.dumps(group,ensure_ascii=False))
        if size+length>max_chars:
            break
        kept.append(group)
        size+=length
    return [message for group in reversed(kept) for message in group]


def delete_session(space, sid):
    with database(space) as db:
        require_session(db, sid)
        db.execute('UPDATE sessions SET deleted=1,memory=\'\' WHERE id=?', (sid,))
        db.execute('DELETE FROM messages WHERE session_id=?', (sid,))
        db.execute('DELETE FROM trait_proposals WHERE session_id=?', (sid,))
        db.execute("UPDATE preparations SET status='superseded' WHERE session_id=?",(sid,))
        from . import library
        library.cancel_obsolete(db)
        db.execute("UPDATE requests SET status='cancelled',result=NULL,message='' WHERE session_id=?", (sid,))
    # Keep a tombstone to reject late completions and retries, but no conversation content.
    path = space_root(space) / 'sessions' / sid / 'memory.md'
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def begin_request(space, sid, rid, message, personalized):
    lease=max(300,float(os.environ.get('VIVIDEO_TURN_TIMEOUT','150'))+30)
    with database(space) as db:
        db.execute('BEGIN IMMEDIATE')
        require_session(db, sid)
        if db.execute('SELECT 1 FROM cancelled_requests WHERE id=?',(rid,)).fetchone():
            raise StateError('请求已取消')
        old = db.execute('SELECT * FROM requests WHERE id=?', (rid,)).fetchone()
        if old:
            if old['session_id'] != sid or old['message'] != message or old['personalized'] != int(personalized):
                raise StateError('请求 ID 已用于不同内容')
            if old['status'] == 'complete':
                from . import library
                result=json.loads(old['result'])
                if library.enabled():result['videos']=library.visible_videos(db,space,result.get('videos',[]))
                return result
            if old['status'] == 'pending' and time.time()-old['started'] < lease:
                raise StateError('请求正在处理，请稍后重试')
            if old['status'] == 'cancelled':
                raise StateError('请求已取消')
            db.execute("UPDATE requests SET status='pending',started=? WHERE id=?", (time.time(), rid))
        else:
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?)', (rid,sid,message,int(personalized),'pending',None,time.time()))
        other = db.execute("SELECT id FROM requests WHERE session_id=? AND status='pending' AND id!=? AND started>?", (sid,rid,time.time()-lease)).fetchone()
        if other:
            raise StateError('当前会话还有请求正在处理')
    return None


def fail_request(space, rid):
    with database(space) as db:
        db.execute("UPDATE requests SET status='failed' WHERE id=? AND status='pending'", (rid,))


def cancel_request(space, rid):
    with database(space) as db:
        db.execute('INSERT OR IGNORE INTO cancelled_requests VALUES (?)',(rid,))
        db.execute("UPDATE requests SET status='cancelled' WHERE id=? AND status='pending'", (rid,))


def assert_request_active(space,rid):
    with database(space) as db:
        row=db.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()
        if row is None or row['status']!='pending':
            raise StateError('请求已取消')


def complete_request(space, sid, rid, message, result, memory, video_links, proposals, protocol=None, config_id=None, preparation=None):
    with database(space) as db:
        db.execute('BEGIN IMMEDIATE')
        session = require_session(db, sid)
        request = db.execute('SELECT * FROM requests WHERE id=?', (rid,)).fetchone()
        if request is None or request['status'] != 'pending':
            raise StateError('请求已取消或完成')
        proposal_rows = []
        for proposal in proposals[:8]:
            # Evidence must quote this user's message, never search output or the model's own text.
            evidence = proposal['evidence'].strip()
            if not proposal['text'].strip() or not evidence or evidence not in message:
                continue
            pid = str(uuid4())
            db.execute('INSERT INTO trait_proposals(id,session_id,request_id,text,evidence) VALUES (?,?,?,?,?)',
                       (pid,sid,rid,proposal['text'],evidence))
            proposal_rows.append(dict(id=pid,session_id=sid,request_id=rid,text=proposal['text'],evidence=evidence,status='pending'))
        result = {**result, 'session_id': sid, 'request_id': rid, 'trait_proposals': proposal_rows, 'demo': False}
        for video in video_links:
            db.execute('INSERT OR IGNORE INTO videos VALUES (?,?,?,?)', (video['id'],video['url'],video.get('query',''),int(time.time()*1000)))
        from . import library
        if library.enabled():
            for video in video_links:
                library.upsert_video(db,video)
            plan=library.commit_plan(db,sid,rid,preparation,message,result.get('videos',[]))
            if plan:
                result['preparation']=library.public_plan(db,plan)
                if plan['phase']=='clarifying':
                    result['videos']=[]
                    if '?' not in result['message'] and '？' not in result['message']:
                        result['message']+='\n\n你更想了解哪个具体方向，已有基础和期望时长是什么？'
                else:
                    local=[]
                    for item in result.get('videos',[]):
                        asset=db.execute("SELECT a.*,v.url FROM media_assets a JOIN videos v ON v.id=a.video_id WHERE video_id=? AND state='ready'",(item['id'],)).fetchone()
                        if asset:local.append(library.video_public(asset,space,item['reason']))
                    result['videos']=local
        db.execute('INSERT INTO messages(session_id,role,content) VALUES (?,\'user\',?)', (sid,message))
        db.execute('INSERT INTO messages(session_id,role,content,payload) VALUES (?,\'assistant\',?,?)', (sid,result['message'],json.dumps({**result,'_protocol_turn':protocol or [],'_config_id':config_id},ensure_ascii=False)))
        db.execute('UPDATE sessions SET title=?,updated=?,memory=?,memory_version=memory_version+1 WHERE id=?',
                   (message[:40] if session['title']=='新对话' else session['title'],int(time.time()*1000),memory,sid))
        db.execute("UPDATE requests SET status='complete',result=? WHERE id=?", (json.dumps(result,ensure_ascii=False),rid))
    export_memories(space, sid)
    return result


def list_videos(space, query=''):
    with database(space) as db:
        return [dict(row) for row in db.execute('SELECT * FROM videos WHERE instr(lower(url||\' \'||query),lower(?))>0 ORDER BY created DESC LIMIT 100', (query,))]


def list_traits(space):
    with database(space) as db:
        return [dict(row) for row in db.execute('SELECT * FROM space_traits ORDER BY created')]


def add_trait(space, text, source=None):
    with database(space) as db:
        db.execute('INSERT OR IGNORE INTO space_traits VALUES (?,?,?,?)', (str(uuid4()),text,source,int(time.time()*1000)))
    export_memories(space)
    return list_traits(space)


def delete_trait(space, tid):
    with database(space) as db:
        if not db.execute('DELETE FROM space_traits WHERE id=?', (tid,)).rowcount:
            raise StateError('特征不存在',404)
    export_memories(space)


def decide_proposal(space, pid, accept, text=None):
    with database(space) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM trait_proposals WHERE id=?', (pid,)).fetchone()
        if row is None:
            raise StateError('特征提议不存在',404)
        require_session(db, row['session_id'])
        if row['status'] != 'pending':
            raise StateError('此特征已经处理')
        if accept:
            db.execute('INSERT OR IGNORE INTO space_traits VALUES (?,?,?,?)', (str(uuid4()),text or row['text'],pid,int(time.time()*1000)))
        db.execute('UPDATE trait_proposals SET status=? WHERE id=?', ('accepted' if accept else 'rejected',pid))
    export_memories(space)
    return list_traits(space)


def atomic_markdown(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.'+str(uuid4())+'.tmp')
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def export_memories(space, sid=None):
    # Read under the writer lock, so delayed exports cannot overwrite newer state.
    with database(space) as db:
        db.execute('BEGIN IMMEDIATE')
        traits = [row['text'] for row in db.execute('SELECT text FROM space_traits ORDER BY created')]
        atomic_markdown(space_root(space)/'memory.md', '# 空间记忆\n\n'+('\n'.join('- '+t for t in traits) or '尚无已确认特征。')+'\n')
        if sid:
            row = db.execute('SELECT * FROM sessions WHERE id=? AND deleted=0', (sid,)).fetchone()
            if row:
                atomic_markdown(space_root(space)/'sessions'/sid/'memory.md', row['memory'] or '# 会话记忆\n\n尚无对话。\n')


def get_video(space,vid):
    with database(space) as db:
        row=db.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone()
        if row is None:
            raise StateError('视频不在当前空间中',404)
        return dict(row)


def record_watch(space,event_id,video_id,session_id,watched,duration):
    with database(space) as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM videos WHERE id=?',(video_id,)).fetchone():
            raise StateError('视频不在当前空间中',404)
        if session_id:
            require_session(db,session_id)
        from . import library
        if library.enabled():
            asset=db.execute('SELECT duration FROM media_assets WHERE video_id=?',(video_id,)).fetchone()
            if asset and asset['duration']>0:
                if abs(duration-asset['duration'])>max(1,asset['duration']*.01):
                    raise StateError('观看记录时长与本地视频不一致',422)
                duration=asset['duration']
        old=db.execute('SELECT * FROM watch_events WHERE id=?',(event_id,)).fetchone()
        if old and (old['video_id']!=video_id or old['session_id']!=session_id or abs(old['duration_seconds']-duration)>1):
            raise StateError('观看记录 ID 已用于不同内容')
        # Exactly 25% is insufficient. No row, timestamp or short-view behavior is saved.
        if watched<=duration/4:
            return {'recorded':False}
        now=int(time.time()*1000)
        if old:
            db.execute('UPDATE watch_events SET watched_seconds=max(watched_seconds,?),updated=? WHERE id=?',(watched,now,event_id))
        else:
            db.execute('INSERT INTO watch_events VALUES (?,?,?,?,?,?,?)',(event_id,video_id,session_id,watched,duration,now,now))
        return {'recorded':True,'event_id':event_id}


def list_watch_events(space):
    with database(space) as db:
        return [dict(row) for row in db.execute('SELECT w.*,v.url FROM watch_events w JOIN videos v ON w.video_id=v.id ORDER BY w.updated DESC LIMIT 100')]
