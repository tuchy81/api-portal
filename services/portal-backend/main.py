"""Portal Backend API — main application."""
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter, Gauge

from routers import catalog, applications, tokens, usage, audit, internal, me, dev_auth
from database import get_pool
import batch

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
app.include_router(internal.router)
app.include_router(me.router, prefix=PREFIX)
app.include_router(dev_auth.router, prefix=PREFIX)

# Prometheus metrics
cdp_active_pat_count = Gauge("cdp_active_pat_count", "Active PAT count", ["status"])
cdp_api_requests_total = Counter("cdp_api_requests_total", "API call count", ["token_id", "api_code", "status"])

@app.on_event("startup")
async def startup():
    await get_pool()
    batch.start_scheduler()
    logger.info("Portal Backend started")

@app.on_event("shutdown")
async def shutdown():
    batch.stop_scheduler()

@app.get("/health")
def health():
    return {"status": "ok", "service": "portal-backend"}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
