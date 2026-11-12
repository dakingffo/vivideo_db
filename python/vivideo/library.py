"""Space-local library, durable jobs, quotas and revision-scoped recommendation plans."""
import json
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4
from hashlib import sha256

from . import storage
from .search import canonical_video_url


def enabled():
    return os.getenv('VIVIDEO_LOCAL_VIDEO', '1') != '0'


def limits():
    return {'quota': int(os.getenv('VIVIDEO_MEDIA_QUOTA_BYTES', str(20*1024**3))),
            'max_file': int(os.getenv('VIVIDEO_VIDEO_MAX_BYTES', str(300*1024**2))),
            'max_duration': int(os.getenv('VIVIDEO_VIDEO_MAX_SECONDS', '1800')),
            'batch': max(1, min(5, int(os.getenv('VIVIDEO_PREFETCH_COUNT', '3'))))}


def root(space):
    return storage.space_root(space)/'media'


def asset_path(space, relative):
    if not relative:
        raise storage.StateError('本地文件尚未准备好', 409)
    base = root(space).resolve()
    path = (base/relative).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise storage.StateError('本地文件不存在', 404)
    return path


def upsert_video(db, row):
    url = canonical_video_url(row.get('url'))
    if not url:
        raise storage.StateError('仅支持 Bilibili、YouTube 和 Vimeo 视频直链', 422)
    existing = db.execute('SELECT id FROM videos WHERE url=?', (url,)).fetchone()
    vid = existing['id'] if existing else 'video_'+sha256(url.encode()).hexdigest()[:24]
    db.execute('INSERT OR IGNORE INTO videos VALUES (?,?,?,?)', (vid,url,row.get('query',''),int(time.time()*1000)))
    db.execute('INSERT OR IGNORE INTO media_assets(video_id,title,summary,author) VALUES (?,?,?,?)',
               (vid,str(row.get('title',''))[:500],str(row.get('summary',''))[:4000],str(row.get('author',''))[:200]))
    db.execute("UPDATE media_assets SET title=CASE WHEN title='' THEN ? ELSE title END,summary=CASE WHEN summary='' THEN ? ELSE summary END WHERE video_id=?",
               (str(row.get('title',''))[:500],str(row.get('summary',''))[:4000],vid))
    return vid


def enqueue(db, kind, target, priority=0, manual=False):
    old = db.execute("SELECT id FROM download_jobs WHERE kind=? AND target=? AND status IN ('queued','running')", (kind,target)).fetchone()
    if old:
        db.execute('UPDATE download_jobs SET priority=max(priority,?),manual=max(manual,?) WHERE id=?', (priority,int(manual),old['id']))
        return old['id']
    jid = str(uuid4()); now = time.time()
    db.execute('INSERT INTO download_jobs(id,kind,target,priority,manual,created,updated) VALUES (?,?,?,?,?,?,?)', (jid,kind,target,priority,int(manual),now,now))
    if kind == 'download':
        db.execute("UPDATE media_assets SET state='queued' WHERE video_id=? AND state!='ready'", (target,))
    return jid


def import_video(space, url, pinned=False):
    with storage.database(space) as db:
        db.execute('BEGIN IMMEDIATE')
        vid = upsert_video(db, {'url':url})
        db.execute('UPDATE media_assets SET pinned=max(pinned,?) WHERE video_id=?', (int(pinned),vid))
        asset = db.execute('SELECT state FROM media_assets WHERE video_id=?', (vid,)).fetchone()
        if asset['state']=='ready':
            row=db.execute('SELECT relative_path FROM media_assets WHERE video_id=?',(vid,)).fetchone()
            try:asset_path(space,row['relative_path'])
            except storage.StateError:db.execute("UPDATE media_assets SET state='evicted',bytes=0 WHERE video_id=?",(vid,))
        current=db.execute('SELECT state FROM media_assets WHERE video_id=?',(vid,)).fetchone()
        jid = None if current['state']=='ready' else enqueue(db,'download',vid,100,manual=True)
    return {'video_id':vid,'job_id':jid}


def latest_plan(db, sid):
    return db.execute("SELECT * FROM preparations WHERE session_id=? AND status!='superseded' ORDER BY revision DESC LIMIT 1", (sid,)).fetchone()


def plan_context(space, sid):
    with storage.database(space) as db:
        plan = latest_plan(db,sid)
        if not plan:
            return {'phase':'new','topic':'','revision':0}
        return public_plan(db,plan)


def public_plan(db, plan):
    rows = db.execute("SELECT a.video_id,a.title,a.state,a.duration FROM plan_candidates c JOIN media_assets a ON a.video_id=c.video_id WHERE c.plan_id=? AND a.state!='evicted'", (plan['id'],)).fetchall()
    return {k:plan[k] for k in ('id','phase','topic','revision','status','error')} | {'candidates':[dict(r) for r in rows]}


def commit_plan(db, sid, rid, draft, message, requested):
    """Called INSIDE the successful chat transaction; no external effects here."""
    old = latest_plan(db,sid)
    first = old is None
    if first and not draft and not requested:
        return None
    # Every new topic must be clarified before video recommendations are accepted.
    new_topic = bool(draft and draft.get('new_topic'))
    replace = first or bool(draft) or (old and old['phase']=='clarifying')
    if replace:
        topic = (draft or {}).get('topic') or (old['topic'] if old else message[:200])
        queries = (draft or {}).get('queries') or [topic + (' '+message[:150] if old else '')]
        queries = [str(q).strip()[:300] for q in queries[:4] if str(q).strip()] or [topic[:300]]
        phase = 'clarifying' if first or new_topic else 'recommending'
        pid = str(uuid4()); revision = (old['revision']+1) if old else 1
        db.execute("UPDATE preparations SET status='superseded' WHERE session_id=?", (sid,))
        db.execute('INSERT INTO preparations(id,session_id,request_id,revision,phase,topic,queries,created) VALUES (?,?,?,?,?,?,?,?)',
                   (pid,sid,rid,revision,phase,topic[:200],json.dumps(queries,ensure_ascii=False),time.time()))
        enqueue(db,'search',pid,50)
        plan = db.execute('SELECT * FROM preparations WHERE id=?',(pid,)).fetchone()
    else:
        plan = old
    # Pending choices belong to this revision and original response, never a later topic.
    chosen = [] if plan['phase']=='clarifying' else requested[:10]
    for item in chosen:
        vid = item['id']
        if not db.execute('SELECT 1 FROM media_assets WHERE video_id=?',(vid,)).fetchone():
            continue
        db.execute('INSERT OR IGNORE INTO plan_candidates VALUES (?,?)',(plan['id'],vid))
        db.execute('INSERT OR REPLACE INTO pending_recommendations VALUES (?,?,?,?)',(plan['id'],rid,vid,item['reason']))
        if db.execute('SELECT state FROM media_assets WHERE video_id=?',(vid,)).fetchone()['state']!='ready':
            enqueue(db,'download',vid,100)
    cancel_obsolete(db)
    schedule_selection(db,plan['id'])
    return plan


def cancel_obsolete(db):
    db.execute("UPDATE download_jobs SET status='cancelled',reserved=0 WHERE kind IN ('search','recommend') AND status IN ('queued','running') AND target IN (SELECT id FROM preparations WHERE status='superseded')")
    # Downloads can be referenced by another active session or explicitly imported.
    db.execute("""UPDATE download_jobs SET status='cancelled' WHERE kind='download' AND status IN ('queued','running') AND manual=0
        AND target NOT IN (SELECT c.video_id FROM plan_candidates c JOIN preparations p ON p.id=c.plan_id JOIN sessions s ON s.id=p.session_id WHERE p.status!='superseded' AND s.deleted=0)
        AND target NOT IN (SELECT video_id FROM media_assets WHERE pinned=1)""")


def video_public(row, space, reason='本地缓存视频'):
    row = dict(row)
    return {'id':row['video_id'],'url':row['url'],'title':row.get('title') or row['url'],
            'author':row.get('author') or '本地视频','summary':row.get('summary',''),
            'duration_seconds':row.get('duration',0),'reason':reason,'local':True,'cache_state':row['state'],
            'category':'视频','level':'','style':'','mark':'VIDEO','subtitle':'','color':'mint' if space=='learning' else 'blue'}


def visible_videos(db, space, items):
    if not enabled():return items
    visible=[]
    for item in items:
        row=db.execute("SELECT a.*,v.url FROM media_assets a JOIN videos v ON v.id=a.video_id WHERE video_id=? AND state='ready'",(item.get('id'),)).fetchone()
        if row:
            try:asset_path(space,row['relative_path'])
            except storage.StateError:continue
            visible.append(video_public(row,space,item.get('reason','本地缓存视频')))
    return visible


def query_videos(space, query='', ready_only=True):
    with storage.database(space) as db:
        rows = db.execute("""SELECT a.*,v.url FROM media_assets a JOIN videos v ON v.id=a.video_id
          WHERE a.state!='evicted' AND (?=0 OR a.state='ready') AND instr(lower(a.title||' '||a.summary||' '||v.query||' '||v.url),lower(?))>0
          ORDER BY a.state='ready' DESC,a.last_access DESC LIMIT 100""", (int(ready_only),query)).fetchall()
        return [video_public(r,space) for r in rows]


def publish(db, space, sid):
    """Refresh only model-approved choices for this session's CURRENT intent."""
    plan = latest_plan(db,sid)
    if not plan or plan['phase']=='clarifying':
        return
    rids = [r[0] for r in db.execute('SELECT DISTINCT request_id FROM pending_recommendations WHERE plan_id=?',(plan['id'],))]
    for rid in rids:
        request = db.execute("SELECT result FROM requests WHERE id=? AND status='complete'",(rid,)).fetchone()
        if not request:
            continue
        result = json.loads(request['result'])
        rows = db.execute("""SELECT a.*,v.url,c.reason FROM pending_recommendations c
          JOIN media_assets a ON a.video_id=c.video_id JOIN videos v ON v.id=a.video_id
          WHERE c.plan_id=? AND c.request_id=? AND a.state='ready' ORDER BY c.rowid""",(plan['id'],rid)).fetchall()
        result['videos'] = [video_public(r,space,r['reason']) for r in rows]
        result['preparation'] = public_plan(db,plan)
        if json.dumps(result,ensure_ascii=False)==request['result']:continue
        db.execute('UPDATE requests SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),rid))
        message = db.execute("SELECT id,payload FROM messages WHERE session_id=? AND role='assistant' AND json_extract(payload,'$.request_id')=?", (sid,rid)).fetchone()
        if message:
            payload = {**json.loads(message['payload']),**result}
            db.execute('UPDATE messages SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),message['id']))


def ready_asset(space, vid, touch=False):
    with storage.database(space) as db:
        row = db.execute('SELECT a.*,v.url FROM media_assets a JOIN videos v ON v.id=a.video_id WHERE video_id=?',(vid,)).fetchone()
        if not row or row['state']!='ready':
            raise storage.StateError('视频正在准备或已清理，请稍后重试',409)
        # Playback is protected from eviction for a renewable lease.
        asset_path(space,row['relative_path'])
        if touch:
            db.execute('UPDATE media_assets SET last_access=?,playback_until=max(playback_until,?) WHERE video_id=?',(time.time(),time.time()+120,vid))
        return dict(row)


def quota(db):
    row = db.execute('SELECT quota_bytes FROM media_settings WHERE id=1').fetchone()
    return row[0] if row else limits()['quota']


def cache_status(space):
    with storage.database(space) as db:
        used = db.execute("SELECT coalesce(sum(bytes),0) FROM media_assets WHERE state='ready'").fetchone()[0]
        reserved = db.execute("SELECT coalesce(sum(reserved),0) FROM download_jobs WHERE reserved>0").fetchone()[0]
        assets = [dict(r) for r in db.execute("SELECT video_id,title,state,bytes,duration,pinned,last_access FROM media_assets WHERE state!='evicted' ORDER BY last_access DESC LIMIT 100")]
        jobs = [dict(r) for r in db.execute('SELECT id,kind,target,status,attempts,progress,error FROM download_jobs ORDER BY created DESC LIMIT 100')]
        return {'quota_bytes':quota(db),'used_bytes':used,'reserved_bytes':reserved,'assets':assets,'jobs':jobs}


def remove_asset(db, space, row):
    if row['playback_until']>time.time():
        raise storage.StateError('视频正在播放，请稍后清理')
    paths = [row[k] for k in ('relative_path','thumbnail_path','subtitle_path') if row[k]]
    directory=None
    if row['relative_path']:
        candidate=(root(space)/row['relative_path']).resolve().parent
        if candidate.parent==(root(space)/'assets').resolve():directory=candidate
    for path in paths:
        try: asset_path(space,path).unlink()
        except storage.StateError: pass
    if directory:shutil.rmtree(directory,ignore_errors=True)
    db.execute("UPDATE media_assets SET state='evicted',relative_path=NULL,thumbnail_path=NULL,subtitle_path=NULL,bytes=0 WHERE video_id=?",(row['video_id'],))


def reserve(db, space, amount, exclude=None):
    used = db.execute("SELECT coalesce(sum(bytes),0) FROM media_assets WHERE state='ready'").fetchone()[0]
    reserved = db.execute("SELECT coalesce(sum(reserved),0) FROM download_jobs WHERE reserved>0").fetchone()[0]
    cap = quota(db)
    rows = db.execute("SELECT * FROM media_assets WHERE state='ready' AND pinned=0 AND playback_until<? AND video_id!=? ORDER BY last_access", (time.time(),exclude or '')).fetchall()
    victims=[]
    for row in rows:
        if used+reserved+amount<=cap:break
        victims.append(row);used-=row['bytes']
    if used+reserved+amount>cap:
        raise storage.StateError('缓存空间不足：请增加配额或取消部分视频保留')
    for row in victims:remove_asset(db,space,row)


def claim_job(space, owner):
    now=time.time()
    with storage.database(space) as db:
        db.execute('BEGIN IMMEDIATE')
        # Process crash: return expired jobs to the queue, discard old worker ownership.
        db.execute("UPDATE download_jobs SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,owner=NULL,reserved=0,error='上次处理被中断，等待恢复' WHERE status='running' AND lease_until<?",(now,))
        db.execute("UPDATE download_jobs SET reserved=0 WHERE status='cancelled' AND lease_until<?",(now,))
        cancel_obsolete(db)
        db.execute("""UPDATE media_assets SET state='failed' WHERE state IN ('queued','downloading','processing')
          AND NOT EXISTS (SELECT 1 FROM download_jobs j WHERE j.kind='download' AND j.target=media_assets.video_id AND j.status IN ('queued','running'))""")
        db.execute("""UPDATE preparations SET status='failed',error='视频检索处理中断，请重新提出或细化需求' WHERE status IN ('queued','searching')
          AND EXISTS (SELECT 1 FROM download_jobs j WHERE j.kind='search' AND j.target=preparations.id AND j.status='failed')
          AND NOT EXISTS (SELECT 1 FROM download_jobs j WHERE j.kind='search' AND j.target=preparations.id AND j.status IN ('queued','running'))""")
        row = db.execute("SELECT * FROM download_jobs WHERE status='queued' AND not_before<=? ORDER BY priority DESC,created LIMIT 1", (now,)).fetchone()
        if not row:return None
        amount = limits()['max_file']*3 if row['kind']=='download' else 0
        try:
            reserve(db,space,amount,row['target'])
        except storage.StateError:
            db.execute("UPDATE download_jobs SET error='缓存空间不足，等待清理',not_before=? WHERE id=?",(now+30,row['id']))
            return None
        db.execute("UPDATE download_jobs SET status='running',owner=?,lease_until=?,attempts=attempts+1,reserved=?,updated=? WHERE id=?",(owner,now+45,amount,now,row['id']))
        return dict(db.execute('SELECT * FROM download_jobs WHERE id=?',(row['id'],)).fetchone())


def heartbeat(space, jid, owner, progress=None):
    with storage.database(space) as db:
        sql = "UPDATE download_jobs SET lease_until=?,updated=?"; args=[time.time()+45,time.time()]
        if progress is not None:sql+=',progress=?';args.append(max(0,min(1,progress)))
        cursor = db.execute(sql+" WHERE id=? AND owner=? AND status='running'",(*args,jid,owner))
        return bool(cursor.rowcount)


def finish_job(space, job, error=None, retry=True):
    with storage.database(space) as db:
        current=db.execute('SELECT * FROM download_jobs WHERE id=?',(job['id'],)).fetchone()
        if not current or current['owner']!=job['owner']:return
        if current['status']=='cancelled':
            db.execute('UPDATE download_jobs SET reserved=0,owner=NULL WHERE id=?',(job['id'],))
            if job['kind']=='download':db.execute("UPDATE media_assets SET state='failed' WHERE video_id=? AND state!='ready' AND NOT EXISTS (SELECT 1 FROM download_jobs WHERE kind='download' AND target=? AND status IN ('queued','running'))",(job['target'],job['target']))
            return
        if current['status']!='running':return
        state = 'queued' if error and retry and current['attempts']<3 else ('failed' if error else 'complete')
        db.execute('UPDATE download_jobs SET status=?,reserved=0,owner=NULL,error=?,not_before=?,updated=?,progress=? WHERE id=?',
                   (state,(error or '')[:200],time.time()+min(60,5*current['attempts']),time.time(),0 if error else 1,job['id']))
        if job['kind']=='download' and error:
            db.execute('UPDATE media_assets SET state=? WHERE video_id=? AND state!=\'ready\'',('queued' if state=='queued' else 'failed',job['target']))
        if job['kind']=='download' and state=='failed':
            # A failed source frees a prefetch slot; try a different known candidate.
            for plan in db.execute("SELECT p.id FROM preparations p JOIN plan_candidates c ON c.plan_id=p.id WHERE c.video_id=? AND p.status!='superseded'",(job['target'],)).fetchall():
                active=db.execute("SELECT count(*) FROM plan_candidates c JOIN media_assets a ON a.video_id=c.video_id WHERE c.plan_id=? AND a.state IN ('ready','queued','downloading','processing')",(plan['id'],)).fetchone()[0]
                for candidate in db.execute("SELECT a.video_id FROM plan_candidates c JOIN media_assets a ON a.video_id=c.video_id WHERE c.plan_id=? AND a.state='missing' LIMIT ?",(plan['id'],max(0,limits()['batch']-active))).fetchall():
                    enqueue(db,'download',candidate['video_id'],30)
        if job['kind']=='recommend' and state=='failed':
            db.execute("UPDATE preparations SET error='视频已缓存，自动筛选暂不可用，可继续对话重新筛选' WHERE id=? AND status!='superseded'",(job['target'],))
        if job['kind']=='search' and error:
            db.execute("UPDATE preparations SET status=?,error=? WHERE id=? AND status!='superseded'", ('queued' if state=='queued' else 'failed',(error or '')[:200],job['target']))


def schedule_selection(db, plan_id):
    plan=db.execute("SELECT * FROM preparations WHERE id=? AND phase='recommending' AND status!='superseded'",(plan_id,)).fetchone()
    if plan and not db.execute('SELECT 1 FROM pending_recommendations WHERE plan_id=?',(plan_id,)).fetchone():
        if db.execute("SELECT 1 FROM plan_candidates c JOIN media_assets a ON a.video_id=c.video_id WHERE c.plan_id=? AND a.state='ready'",(plan_id,)).fetchone():
            enqueue(db,'recommend',plan_id,60)


def session_preparation(space, sid):
    with storage.database(space) as db:
        db.execute('BEGIN IMMEDIATE');storage.require_session(db,sid);publish(db,space,sid)
        plan=latest_plan(db,sid)
        # Return only the original response identities and public video snapshots.
        turns=[]
        for row in db.execute("SELECT payload FROM messages WHERE session_id=? AND role='assistant'",(sid,)):
            payload=json.loads(row['payload']);turns.append({'request_id':payload.get('request_id'),'videos':visible_videos(db,space,payload.get('videos',[]))})
        return {'preparation':public_plan(db,plan) if plan else None,'turns':turns}


def reconcile(space):
    """Crash recovery: READY always means verified files still exist; old staging is bounded."""
    now=time.time(); staging=root(space)/'staging';staging.mkdir(parents=True,exist_ok=True)
    with storage.database(space) as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute('SELECT * FROM videos WHERE id NOT IN (SELECT video_id FROM media_assets)').fetchall():upsert_video(db,dict(row))
        for row in db.execute("SELECT * FROM media_assets WHERE state='ready'").fetchall():
            try:asset_path(space,row['relative_path'])
            except storage.StateError:db.execute("UPDATE media_assets SET state='evicted',bytes=0 WHERE video_id=?",(row['video_id'],))
        active={r['id']:r['owner'] for r in db.execute("SELECT id,owner FROM download_jobs WHERE status IN ('running','queued')")}
        for path in staging.iterdir():
            if path.name not in active and now-path.stat().st_mtime>60:shutil.rmtree(path,ignore_errors=True)
            elif path.is_dir():
                for attempt in path.iterdir():
                    if attempt.is_dir() and attempt.name!=active.get(path.name) and now-attempt.stat().st_mtime>60:shutil.rmtree(attempt,ignore_errors=True)
        known={r[0] for r in db.execute("SELECT relative_path FROM media_assets WHERE state='ready'")}
        assets=root(space)/'assets'
        if assets.exists():
            for directory in assets.iterdir():
                if directory.is_dir() and not any(str(p.relative_to(root(space))) in known for p in directory.iterdir()) and now-directory.stat().st_mtime>3600:
                    shutil.rmtree(directory,ignore_errors=True)
