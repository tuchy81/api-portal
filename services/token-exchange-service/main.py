"""Token Exchange Service — RFC 8693, JWT cache, circuit breaker."""
import time, logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

import redis_client as rc
import circuit_breaker as cb_module
import cache as jwt_cache
import metrics as m
from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("txs")

app = FastAPI(title="Token Exchange Service")

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    r = rc.get_redis()
    rc.load_lua_scripts(r)
    logger.info("TXS started. Redis connected.")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ExchangeRequest(BaseModel):
    tokenId: str
    userSub: str
    scopes: list[str]

class ExchangeResponse(BaseModel):
    accessToken: str
    expiresIn: int
    jti: str
    cached: bool

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/internal/token-exchange", response_model=ExchangeResponse)
async def token_exchange(req: ExchangeRequest, request: Request):
    r = rc.get_redis()

    # Circuit breaker check
    if cb_module.is_circuit_open(r):
        # Check if there's a cached JWT we can still use
        import hashlib
        sh = hashlib.sha256(" ".join(sorted(req.scopes)).encode()).hexdigest()[:8]
        cached_jwt = r.get(f"cdp:jwt:{req.tokenId}:{sh}")
        if cached_jwt:
            from keycloak_client import _extract_jti
            m.token_exchange_total.labels(result="hit").inc()
            return ExchangeResponse(
                accessToken=cached_jwt,
                expiresIn=settings.jwt_cache_ttl,
                jti=_extract_jti(cached_jwt),
                cached=True,
            )
        return JSONResponse(
            {"type": "https://cdp-portal.hd.com/errors/CDP-2001",
             "title": "Authorization service unavailable",
             "status": 503, "code": "CDP-2001",
             "detail": "Circuit breaker is OPEN and no cached JWT available"},
            status_code=503
        )

    start = time.perf_counter()
    try:
        jwt_str, was_cached = await jwt_cache.get_or_exchange_jwt(
            req.tokenId, req.userSub, req.scopes, r, settings.node_id
        )
        elapsed = time.perf_counter() - start
        m.token_exchange_latency.observe(elapsed)
        m.token_exchange_total.labels(result="hit" if was_cached else "miss").inc()
        cb_module.record_success(r)

        from keycloak_client import _extract_jti
        return ExchangeResponse(
            accessToken=jwt_str,
            expiresIn=settings.jwt_ttl if not was_cached else settings.jwt_cache_ttl,
            jti=_extract_jti(jwt_str),
            cached=was_cached,
        )
    except TimeoutError as e:
        m.token_exchange_total.labels(result="fail").inc()
        cb_module.record_failure(r)
        return JSONResponse(
            {"type": "https://cdp-portal.hd.com/errors/CDP-2001",
             "title": "Token exchange timeout",
             "status": 503, "code": "CDP-2001", "detail": str(e)},
            status_code=503
        )
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        m.token_exchange_total.labels(result="fail").inc()
        cb_module.record_failure(r)
        return JSONResponse(
            {"type": "https://cdp-portal.hd.com/errors/CDP-2001",
             "title": "Authorization service unavailable",
             "status": 503, "code": "CDP-2001", "detail": str(e)},
            status_code=503
        )

@app.get("/health")
def health():
    r = rc.get_redis()
    circuit_state = r.get("cdp:cb:txs") or "CLOSED"
    return {"status": "ok", "service": "token-exchange-service", "circuit": circuit_state}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
