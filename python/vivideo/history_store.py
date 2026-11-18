import sqlite3
from typing import Optional, List, Dict, Any

# ================= SQL 语句定义 =================

SQL_COUNT_HISTORY = """
SELECT COUNT(*)
FROM watch_events we
JOIN videos v ON we.video_id = v.id
LEFT JOIN media_assets ma ON v.id = ma.video_id
WHERE we.created >= ? AND we.created < ?
  AND (? IS NULL OR instr(COALESCE(NULLIF(ma.title, ''), v.id), ?) > 0);
"""

SQL_LIST_HISTORY = """
SELECT 
    we.id, we.video_id, we.session_id, 
    we.watched_seconds, we.duration_seconds, 
    we.created, we.updated,
    COALESCE(NULLIF(ma.title, ''), v.id) AS title,
    MIN(we.watched_seconds, we.duration_seconds) AS counted_seconds,
    MIN(CAST(we.watched_seconds AS REAL) / we.duration_seconds, 1.0) AS progress
FROM watch_events we
JOIN videos v ON we.video_id = v.id
LEFT JOIN media_assets ma ON v.id = ma.video_id
WHERE we.created >= ? AND we.created < ?
  AND (? IS NULL OR instr(COALESCE(NULLIF(ma.title, ''), v.id), ?) > 0)
ORDER BY we.created DESC, we.id ASC
LIMIT ? OFFSET ?;
"""

SQL_STATS_OVERVIEW = """
SELECT 
    COUNT(*) AS event_count,
    COUNT(DISTINCT we.video_id) AS video_count,
    COALESCE(SUM(MIN(we.watched_seconds, we.duration_seconds)), 0) AS watched_seconds
FROM watch_events we
WHERE we.created >= ? AND we.created < ?;
"""

SQL_STATS_RANKING = """
SELECT 
    we.video_id,
    COALESCE(NULLIF(ma.title, ''), v.id) AS title,
    COUNT(*) AS event_count,
    SUM(MIN(we.watched_seconds, we.duration_seconds)) AS watched_seconds
FROM watch_events we
JOIN videos v ON we.video_id = v.id
LEFT JOIN media_assets ma ON v.id = ma.video_id
WHERE we.created >= ? AND we.created < ?
GROUP BY we.video_id
ORDER BY watched_seconds DESC, event_count DESC, we.video_id ASC
LIMIT 5;
"""

SQL_DAILY_STATS = """
SELECT 
    date(we.created / 1000.0, 'unixepoch', '+8 hours') AS day,
    COUNT(*) AS event_count,
    COUNT(DISTINCT we.video_id) AS video_count,
    SUM(MIN(we.watched_seconds, we.duration_seconds)) AS watched_seconds
FROM watch_events we
WHERE we.created >= ? AND we.created < ?
GROUP BY day
ORDER BY day;
"""

SQL_DELETE_EVENT = "DELETE FROM watch_events WHERE id = ?;"

# ================= 核心函数实现 =================

def ensure_schema(db: sqlite3.Connection):
    """初始化索引，逐条执行，不在业务事务中"""
    db.execute("CREATE INDEX IF NOT EXISTS idx_watch_events_created ON watch_events(created DESC, id);")
    db.execute("CREATE INDEX IF NOT EXISTS idx_watch_events_video_created ON watch_events(video_id, created DESC, id);")

def _execute_read_transaction(db: sqlite3.Connection, queries: List[tuple]) -> List[Any]:
    """在同一个只读事务中执行多个查询，保证快照一致性"""
    db.execute("BEGIN DEFERRED")
    try:
        results = []
        for sql, params in queries:
            cursor = db.execute(sql, params)
            results.append(cursor.fetchall() if sql.strip().upper().startswith("SELECT") else cursor.rowcount)
        db.execute("COMMIT")
        return results
    except Exception:
        db.execute("ROLLBACK")
        raise

def get_history_page(db: sqlite3.Connection, from_ms: int, to_ms: int, keyword: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
    ensure_schema(db)
    like_kw = keyword if keyword else None
    
    count_res, list_res = _execute_read_transaction(db, [
        (SQL_COUNT_HISTORY, (from_ms, to_ms, like_kw, like_kw)),
        (SQL_LIST_HISTORY, (from_ms, to_ms, like_kw, like_kw, limit, offset))
    ])
    
    total = count_res[0][0]
    items = [
        {
            "id": row[0], "video_id": row[1], "session_id": row[2],
            "watched_seconds": row[3], "duration_seconds": row[4],
            "created": row[5], "updated": row[6], "title": row[7],
            "counted_seconds": row[8], "progress": row[9]
        } for row in list_res
    ]
    return {"total": total, "items": items}

def delete_history_event(db: sqlite3.Connection, event_id: str) -> bool:
    ensure_schema(db)
    cursor = db.execute(SQL_DELETE_EVENT, (event_id,))
    return cursor.rowcount > 0

def get_stats(db: sqlite3.Connection, from_ms: int, to_ms: int) -> Dict[str, Any]:
    ensure_schema(db)
    overview_res, ranking_res = _execute_read_transaction(db, [
        (SQL_STATS_OVERVIEW, (from_ms, to_ms)),
        (SQL_STATS_RANKING, (from_ms, to_ms))
    ])
    
    ov = overview_res[0]
    event_count = ov[0]
    watched_seconds = ov[2]
    
    return {
        "event_count": event_count,
        "video_count": ov[1],
        "watched_seconds": watched_seconds,
        "average_seconds": (watched_seconds / event_count) if event_count > 0 else 0,
        "ranking": [
            {"video_id": r[0], "title": r[1], "event_count": r[2], "watched_seconds": r[3]}
            for r in ranking_res
        ]
    }

def get_daily_stats(db: sqlite3.Connection, from_ms: int, to_ms: int) -> List[Dict[str, Any]]:
    ensure_schema(db)
    res = _execute_read_transaction(db, [(SQL_DAILY_STATS, (from_ms, to_ms))])[0]
    return [
        {"day": r[0], "event_count": r[1], "video_count": r[2], "watched_seconds": r[3]}
        for r in res
    ]