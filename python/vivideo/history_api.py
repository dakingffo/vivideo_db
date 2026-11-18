from fastapi import APIRouter, Query, HTTPException, Response
from typing import Optional
from datetime import datetime, timezone, timedelta
import uuid

from . import storage
from .storage import StateError  # 从主项目引入异常
from .history_store import (
    get_history_page, delete_history_event, 
    get_stats, get_daily_stats
)

router = APIRouter(prefix="/api/spaces/{space_id}")
BJT = timezone(timedelta(hours=8))

def validate_space(space_id: str):
    if space_id not in ("learning", "roaming"):
        raise StateError("Invalid space_id", 422)

def validate_uuid(event_id: str):
    try:
        uuid.UUID(event_id)
    except ValueError:
        raise StateError("Invalid UUID format", 422)

def parse_time_range(from_ms: Optional[int], to_ms: Optional[int]) -> tuple[int, int]:
    if (from_ms is None) != (to_ms is None):
        raise StateError("from_ms and to_ms must be provided together", 422)
    
    if from_ms is None:
        now_bjt = datetime.now(BJT)
        today_start = now_bjt.replace(hour=0, minute=0, second=0, microsecond=0)
        from_dt = today_start - timedelta(days=6)
        to_dt = today_start + timedelta(days=1)
        from_ms = int(from_dt.timestamp() * 1000)
        to_ms = int(to_dt.timestamp() * 1000)
    else:
        if not (0 <= from_ms < to_ms):
            raise StateError("Invalid time range: must be 0 <= from_ms < to_ms", 422)
        if (to_ms - from_ms) > 31 * 86400000:
            raise StateError("Time span exceeds 31 days", 422)
            
    try:
        datetime.fromtimestamp(from_ms / 1000, tz=BJT)
        datetime.fromtimestamp(to_ms / 1000, tz=BJT)
    except (ValueError, OverflowError, OSError):
        raise StateError("Invalid timestamp", 422)
    return from_ms, to_ms

@router.get("/watch-history")
def get_history(
    space_id: str,
    from_ms: Optional[int] = Query(None),
    to_ms: Optional[int] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    validate_space(space_id)
    from_ms, to_ms = parse_time_range(from_ms, to_ms)
    
    keyword = None
    if q is not None:
        q = q.strip()
        if len(q) > 100:
            raise StateError("Keyword exceeds 100 characters", 422)
        if q:
            keyword = q

    with storage.database(space_id) as db:
        data = get_history_page(db, from_ms, to_ms, keyword, limit, offset)
        
    return {
        "items": data["items"],
        "total": data["total"],
        "limit": limit,
        "offset": offset,
        "from_ms": from_ms,
        "to_ms": to_ms
    }

@router.delete("/watch-history/{event_id}")
def delete_history(space_id: str, event_id: str):
    validate_space(space_id)
    validate_uuid(event_id)
    
    with storage.database(space_id) as db:
        success = delete_history_event(db, event_id)
        
    if not success:
        raise HTTPException(status_code=404, detail="Event not found")
    # 这里改成了 204 无响应体
    return Response(status_code=204)

@router.get("/watch-stats")
def get_watch_stats(
    space_id: str,
    from_ms: Optional[int] = Query(None),
    to_ms: Optional[int] = Query(None)
):
    validate_space(space_id)
    from_ms, to_ms = parse_time_range(from_ms, to_ms)
    
    with storage.database(space_id) as db:
        stats = get_stats(db, from_ms, to_ms)
        
    return {
        "from_ms": from_ms,
        "to_ms": to_ms,
        "event_count": stats["event_count"],
        "video_count": stats["video_count"],
        "watched_seconds": stats["watched_seconds"],
        "average_seconds": stats["average_seconds"],
        "top_videos": stats["ranking"]
    }

@router.get("/watch-stats/daily")
def get_daily_watch_stats(
    space_id: str,
    from_ms: Optional[int] = Query(None),
    to_ms: Optional[int] = Query(None)
):
    validate_space(space_id)
    from_ms, to_ms = parse_time_range(from_ms, to_ms)
    
    with storage.database(space_id) as db:
        raw_daily = get_daily_stats(db, from_ms, to_ms)
        
    # 无记录日补零逻辑
    daily_map = {item["date"]: item for item in raw_daily}
    from_dt = datetime.fromtimestamp(from_ms / 1000.0, tz=BJT).replace(hour=0, minute=0, second=0, microsecond=0)
    to_dt = datetime.fromtimestamp(to_ms / 1000.0, tz=BJT).replace(hour=0, minute=0, second=0, microsecond=0)
    
    result = []
    current_dt = from_dt
    while int(current_dt.timestamp() * 1000) < to_ms:
        day_str = current_dt.strftime("%Y-%m-%d")
        if day_str in daily_map:
            result.append(daily_map[day_str])
        else:
            result.append({
                "date": day_str,
                "event_count": 0,
                "video_count": 0,
                "watched_seconds": 0
            })
        current_dt += timedelta(days=1)
        
    return {
        "from_ms": from_ms,
        "to_ms": to_ms,
        "timezone": "UTC+08:00",
        "items": result
    }