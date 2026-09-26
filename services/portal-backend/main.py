"""Portal Backend API — main application."""
import asyncio
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter, Gauge

from routers import catalog, applications, tokens, usage, audit, internal, me, dev_auth, admin_tokens
from config import settings
from database import get_pool
import apisix_client
import batch
import audit_consumer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("portal")

app = FastAPI(title="Citizen Developer API Portal Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
PREFIX = "/portal/v1"
app.include_router(catalog.router, prefix=PREFIX)
app.include_router(applications.router, prefix=PREFIX)
app.include_router(tokens.router, prefix=PREFIX)
app.include_router(usage.router, prefix=PREFIX)
app.include_router(audit.router, prefix=PREFIX)
app.include_router(admin_tokens.router, prefix=PREFIX)
app.include_router(internal.router)
app.include_router(me.router, prefix=PREFIX)
if settings.enable_dev_auth:
    # dev-login mints a signed JWT for any user id with no credentials, so the
    # routes only exist when explicitly switched on (local compose).
    app.include_router(dev_auth.router, prefix=PREFIX)
    logger.warning("dev auth endpoints are ENABLED — never do this outside local development")

# Prometheus metrics
cdp_active_pat_count = Gauge("cdp_active_pat_count", "Active PAT count", ["status"])
cdp_api_requests_total = Counter("cdp_api_requests_total", "API call count", ["token_id", "api_code", "status"])

async def resync_gateway_routes():
    """Re-PUT every PUBLISHED catalog entry's APISIX route on startup —
    etcd starts empty, so this is what repopulates it (spec section 8's
    registration pipeline is what keeps it populated afterwards). Runs as a
    background task with its own retry/backoff per route rather than
    blocking app startup: there's no depends_on from portal-backend to
    citizen-gateway (that would cycle back, since citizen-gateway already
    depends on portal-backend), so APISIX/etcd may not be reachable yet
    when this first runs.
    """
    pool = await get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch("SELECT * FROM cdp.api_catalog WHERE status='PUBLISHED'")
        scopes_by_api = {
            row["api_id"]: await db.fetch("SELECT * FROM cdp.api_scope WHERE api_id=$1", row["api_id"])
            for row in rows
        }

    for row in rows:
        api_row = {"api_code": row["api_code"], "upstream_url": row["upstream_url"], "public_path": row["public_path"]}
        scope_rows = [
            {"scope_name": s["scope_name"], "http_method": s["http_method"], "path_pattern": s["path_pattern"]}
            for s in scopes_by_api[row["api_id"]]
        ]
        for attempt in range(10):
            try:
                await apisix_client.upsert_route(api_row, scope_rows)
                break
            except Exception as e:
                if attempt == 9:
                    logger.error(f"gateway route resync gave up for {row['api_code']}: {e}")
                else:
                    await asyncio.sleep(2)
    logger.info(f"Gateway route resync attempted for {len(rows)} published APIs")

_audit_consumer_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup():
    global _audit_consumer_task
    await get_pool()
    batch.start_scheduler()
    asyncio.create_task(resync_gateway_routes())
    # pat-audit.lua XADDs security events to cdp:audit:security; this task
    # drains that stream into cdp.audit_log so worker restarts / portal HTTP
    # /internal/audit downtime don't lose those events.
    _audit_consumer_task = asyncio.create_task(audit_consumer.run_consumer())
    logger.info("Portal Backend started")

@app.on_event("shutdown")
async def shutdown():
    batch.stop_scheduler()
    if _audit_consumer_task and not _audit_consumer_task.done():
        _audit_consumer_task.cancel()
        try:
            await _audit_consumer_task
        except asyncio.CancelledError:
            pass

@app.get("/health")
def health():
    return {"status": "ok", "service": "portal-backend"}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
