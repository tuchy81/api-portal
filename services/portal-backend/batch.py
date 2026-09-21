"""Batch jobs (BAT-CDP-01, 03, 04, 06)."""
import asyncio, logging
from datetime import date, timedelta, datetime, timezone
import redis as redis_lib
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("portal.batch")
_scheduler: AsyncIOScheduler = None

async def _get_db_pool():
    from database import get_pool
    return await get_pool()

def _get_redis():
    import redis_client as rc
    return rc.get_redis()

def _acquire_lock(r: redis_lib.Redis, job_id: str, ttl: int = 3600) -> bool:
    return bool(r.set(f"cdp:batch:lock:{job_id}", "1", nx=True, ex=ttl))

async def bat_cdp_01():
    """Daily: aggregate audit_log → usage_stat_daily."""
    r = _get_redis()
    if not _acquire_lock(r, "BAT-CDP-01"):
        return
    try:
        pool = await _get_db_pool()
        yesterday = date.today() - timedelta(days=1)
        async with pool.acquire() as db:
            await db.execute(
                """INSERT INTO cdp.usage_stat_daily
                       (stat_date, token_id, api_id, call_count, error_count, avg_latency, p95_latency)
                   SELECT $1, al.token_id, c.api_id,
                          COUNT(*) FILTER (WHERE al.status_code < 400),
                          COUNT(*) FILTER (WHERE al.status_code >= 400),
                          COALESCE(ROUND(AVG(al.latency_ms))::int, 0),
                          COALESCE(
                              PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY al.latency_ms)::int, 0)
                   FROM cdp.audit_log al
                   JOIN cdp.pat p ON al.token_id = p.token_id
                   JOIN cdp.application a ON p.app_id = a.app_id
                   JOIN cdp.api_catalog c ON a.api_id = c.api_id
                   WHERE al.occurred_at::date = $1 AND al.token_id IS NOT NULL
                   GROUP BY al.token_id, c.api_id
                   ON CONFLICT (stat_date, token_id, api_id) DO UPDATE
                   SET call_count = EXCLUDED.call_count, error_count = EXCLUDED.error_count,
                       avg_latency = EXCLUDED.avg_latency, p95_latency = EXCLUDED.p95_latency""",
                yesterday
            )
        logger.info(f"BAT-CDP-01: aggregated stats for {yesterday}")
    except Exception as e:
        logger.error(f"BAT-CDP-01 failed: {e}")
    finally:
        r.delete("cdp:batch:lock:BAT-CDP-01")

async def bat_cdp_03():
    """Daily: expire PATs past expires_at."""
    r = _get_redis()
    if not _acquire_lock(r, "BAT-CDP-03"):
        return
    try:
        pool = await _get_db_pool()
        async with pool.acquire() as db:
            expired = await db.fetch(
                "SELECT token_id FROM cdp.pat WHERE status='ACTIVE' AND expires_at < now()"
            )
            for row in expired:
                await db.execute(
                    "UPDATE cdp.pat SET status='EXPIRED' WHERE token_id=$1", row["token_id"]
                )
                import redis_client as rc_module
                rc_module.invalidate_pat(r, row["token_id"])
        logger.info(f"BAT-CDP-03: expired {len(expired)} PATs")
    except Exception as e:
        logger.error(f"BAT-CDP-03 failed: {e}")
    finally:
        r.delete("cdp:batch:lock:BAT-CDP-03")

async def bat_cdp_04():
    """Daily: mark 30-day unused PATs as INACTIVE."""
    r = _get_redis()
    if not _acquire_lock(r, "BAT-CDP-04"):
        return
    try:
        pool = await _get_db_pool()
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        async with pool.acquire() as db:
            inactive = await db.fetch(
                """SELECT token_id FROM cdp.pat
                   WHERE status='ACTIVE' AND (last_used_at IS NULL OR last_used_at < $1)
                   AND issued_at < $1""",
                cutoff
            )
            for row in inactive:
                await db.execute(
                    "UPDATE cdp.pat SET status='INACTIVE' WHERE token_id=$1", row["token_id"]
                )
                import redis_client as rc_module
                rc_module.invalidate_pat(r, row["token_id"])
        logger.info(f"BAT-CDP-04: inactivated {len(inactive)} PATs")
    except Exception as e:
        logger.error(f"BAT-CDP-04 failed: {e}")
    finally:
        r.delete("cdp:batch:lock:BAT-CDP-04")

async def bat_cdp_06():
    """Monthly: create audit_log partitions two months ahead.

    Without this the table runs out of partitions and every audit insert
    fails. A DEFAULT partition exists as a safety net, but rows landing there
    block later CREATE ... PARTITION OF for that month, so this must keep
    running ahead of time rather than catching up afterwards.
    """
    r = _get_redis()
    if not _acquire_lock(r, "BAT-CDP-06"):
        return
    try:
        pool = await _get_db_pool()
        today = date.today()
        async with pool.acquire() as db:
            for offset in (1, 2):
                month = today.month + offset
                year = today.year + (month - 1) // 12
                month = (month - 1) % 12 + 1
                start = date(year, month, 1)
                end = date(year + (month // 12), month % 12 + 1, 1)
                name = f"audit_log_{year}_{month:02d}"
                try:
                    await db.execute(
                        f"""CREATE TABLE IF NOT EXISTS cdp.{name}
                            PARTITION OF cdp.audit_log
                            FOR VALUES FROM ('{start}') TO ('{end}')"""
                    )
                except Exception as e:
                    # Most likely cause: rows for this month already sit in the
                    # DEFAULT partition, i.e. this batch previously missed a run.
                    logger.error(f"BAT-CDP-06: could not create {name}: {e}")
        logger.info("BAT-CDP-06: partition check complete")
    except Exception as e:
        logger.error(f"BAT-CDP-06 failed: {e}")
    finally:
        r.delete("cdp:batch:lock:BAT-CDP-06")

def start_scheduler():
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(bat_cdp_01, "cron", hour=0, minute=5, id="BAT-CDP-01")
    _scheduler.add_job(bat_cdp_03, "cron", hour=2, minute=0, id="BAT-CDP-03")
    _scheduler.add_job(bat_cdp_04, "cron", hour=2, minute=30, id="BAT-CDP-04")
    _scheduler.add_job(bat_cdp_06, "cron", day=1, hour=1, minute=0, id="BAT-CDP-06")
    _scheduler.start()
    logger.info("Batch scheduler started")

def stop_scheduler():
    if _scheduler:
        _scheduler.shutdown()
