"""Additive schema for durable, space-local video preparation."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS media_assets (
 video_id TEXT PRIMARY KEY REFERENCES videos(id), title TEXT NOT NULL DEFAULT '',
 summary TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'missing', relative_path TEXT, thumbnail_path TEXT, subtitle_path TEXT,
 duration REAL NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
 pinned INTEGER NOT NULL DEFAULT 0, last_access REAL NOT NULL DEFAULT 0,
 playback_until REAL NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS preparations (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT NOT NULL,
 revision INTEGER NOT NULL, phase TEXT NOT NULL, topic TEXT NOT NULL, queries TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
 UNIQUE(session_id, revision)
);
CREATE TABLE IF NOT EXISTS plan_candidates (
 plan_id TEXT NOT NULL REFERENCES preparations(id), video_id TEXT NOT NULL REFERENCES videos(id),
 PRIMARY KEY(plan_id, video_id)
);
CREATE TABLE IF NOT EXISTS download_jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 priority INTEGER NOT NULL DEFAULT 0, manual INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 owner TEXT, lease_until REAL NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0,
 reserved INTEGER NOT NULL DEFAULT 0, progress REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_media_job ON download_jobs(kind,target)
 WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS pending_recommendations (
 plan_id TEXT NOT NULL REFERENCES preparations(id), request_id TEXT NOT NULL,
 video_id TEXT NOT NULL REFERENCES videos(id), reason TEXT NOT NULL,
 PRIMARY KEY(plan_id,request_id,video_id)
);
CREATE TABLE IF NOT EXISTS media_settings (
 id INTEGER PRIMARY KEY CHECK(id=1), quota_bytes INTEGER NOT NULL
);
'''
