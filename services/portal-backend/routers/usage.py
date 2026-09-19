"""Usage statistics endpoints."""
import uuid, logging
from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
import asyncpg
from database import get_db
from auth import get_current_user, UserClaims
import redis_client as rc

logger = logging.getLogger("portal.usage")
router = APIRouter(prefix="/usage", tags=["usage"])

@router.get("/tokens/{token_id}")
async def get_token_usage(
    token_id: str,
    days: int = Query(30, ge=1, le=90),
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    pat = await db.fetchrow("SELECT * FROM cdp.pat WHERE token_id=$1", token_id)
    if not pat:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "PAT not found"})

    is_admin = any(r in user.roles for r in ["platform-admin"])
    if pat["user_sub"] != user.sub and not is_admin:
        raise HTTPException(403, detail={"code": "CDP-4003", "message": "Access denied"})

    since = date.today() - timedelta(days=days)
    rows = await db.fetch(
        """SELECT stat_date, call_count, error_count, avg_latency, p95_latency
           FROM cdp.usage_stat_daily
           WHERE token_id=$1 AND stat_date >= $2
           ORDER BY stat_date""",
        token_id, since
    )

    # Live counter from Redis
    r = rc.get_redis()
    today_str = date.today().strftime("%Y%m%d")
    month_str = date.today().strftime("%Y%m")
    live_daily = int(r.get(f"cdp:quota:d:{token_id}:{today_str}") or 0)
    live_monthly = int(r.get(f"cdp:quota:m:{token_id}:{month_str}") or 0)

    quota = await db.fetchrow("SELECT * FROM cdp.quota_policy WHERE token_id=$1", token_id)

    return {
        "tokenId": token_id,
        "dailySeries": [
            {"date": str(r["stat_date"]), "calls": r["call_count"],
             "errors": r["error_count"], "avgLatencyMs": r["avg_latency"]}
            for r in rows
        ],
        "liveToday": {"calls": live_daily, "quota": quota["daily_quota"] if quota else 5000},
        "liveMonth": {"calls": live_monthly, "quota": quota["monthly_quota"] if quota else 100000},
    }
