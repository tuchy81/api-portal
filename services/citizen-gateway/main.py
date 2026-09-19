"""Citizen API Gateway — PAT auth + quota + token exchange + proxy."""
import time, uuid, logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, Counter, Histogram
import httpx

import redis_client as rc
import scope_config
from plugins import pat_auth, quota as quota_plugin, token_exchange, audit
from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway")

app = FastAPI(title="Citizen API Gateway")

cdp_api_requests_total = Counter("cdp_api_requests_total", "API requests", ["token_id", "status_code"])
cdp_api_latency_seconds = Histogram("cdp_api_latency_seconds", "API latency", ["api_code"])
cdp_quota_rejected_total = Counter("cdp_quota_rejected_total", "Quota rejections", ["token_id", "reason"])

def _problem_json(code: str, title: str, status: int, detail: str, instance: str = "/", retry_after: int = None) -> JSONResponse:
    body = {
        "type": f"https://cdp-portal.hd.com/errors/{code}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        "instance": instance,
    }
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)

@app.on_event("startup")
async def startup():
    r = rc.get_redis()
    rc.load_quota_script(r)
    logger.info("Citizen Gateway started")

@app.on_event("shutdown")
async def shutdown():
    await audit.flush_all()

@app.get("/health")
def health():
    return {"status": "ok", "service": "citizen-gateway"}

@app.get("/metrics")
def metrics_endpoint():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.api_route("/capi/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def gateway_handler(request: Request, path: str):
    start = time.perf_counter()
    trace_id = str(uuid.uuid4()).replace("-", "")[:32]
    full_path = "/" + path if not path.startswith("/") else path
    # Reconstruct CAPI path
    capi_path = f"/capi/{path}"
    if request.url.query:
        capi_path_with_query = f"{capi_path}?{request.url.query}"
    else:
        capi_path_with_query = capi_path

    r = rc.get_redis()
    authorization = request.headers.get("Authorization", "")
    client_ip = request.client.host if request.client else "0.0.0.0"
    # Check X-Forwarded-For for real IP
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        client_ip = xff.split(",")[0].strip()

    # Route matching
    method = request.method
    route = scope_config.match_route(method, capi_path)
    if not route:
        return _problem_json("CDP-2002", "Route not found", 404, f"No route for {method} {capi_path}", capi_path)

    required_scope = route["scope"]
    upstream_path = scope_config.get_upstream_path(capi_path, route)
    if request.url.query:
        upstream_path = f"{upstream_path}?{request.url.query}"

    # ---- Phase 1: PAT Authentication ----
    pat_ctx = None
    try:
        pat_ctx = await pat_auth.authenticate_pat(authorization, client_ip, method, capi_path, required_scope, r)
    except pat_auth.AuthError as e:
        await audit.record_event(
            "AUTH_FAILED" if e.code in ("CDP-1001", "CDP-1002") else "SCOPE_DENIED",
            None, None, None, trace_id, client_ip, method, capi_path, e.status, None, e.code
        )
        cdp_api_requests_total.labels(token_id="unknown", status_code=e.status).inc()
        return _problem_json(e.code, e.message, e.status, e.message, capi_path)

    token_id = pat_ctx["token_id"]

    # ---- Phase 2: Quota & Rate Limit ----
    quota_headers = {}
    try:
        quota_headers = quota_plugin.deduct_quota(r, pat_ctx)
    except quota_plugin.QuotaError as e:
        await audit.record_event(
            "QUOTA_EXCEEDED", token_id, pat_ctx["user_sub"], None,
            trace_id, client_ip, method, capi_path, 429, None, "CDP-1004"
        )
        cdp_quota_rejected_total.labels(token_id=token_id, reason=e.reason).inc()
        cdp_api_requests_total.labels(token_id=token_id, status_code=429).inc()
        return _problem_json("CDP-1004", "Quota exceeded", 429,
                             f"Quota exceeded: {e.reason}", capi_path, retry_after=e.retry_after)

    # ---- Phase 3: Token Exchange ----
    jwt = None
    try:
        jwt = await token_exchange.get_jwt(pat_ctx, r)
    except Exception as e:
        await audit.record_event(
            "EXCHANGE_FAILED", token_id, pat_ctx["user_sub"], None,
            trace_id, client_ip, method, capi_path, 503, None, "CDP-2001"
        )
        cdp_api_requests_total.labels(token_id=token_id, status_code=503).inc()
        return _problem_json("CDP-2001", "Authorization service unavailable", 503, str(e), capi_path, retry_after=30)

    # ---- Phase 4: Upstream Proxy ----
    upstream_url = settings.internal_gw_url + upstream_path
    body = await request.body()

    upstream_headers = token_exchange.build_upstream_headers(
        dict(request.headers), jwt, pat_ctx, trace_id
    )
    # Remove host header
    upstream_headers.pop("host", None)
    upstream_headers.pop("Host", None)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method=method,
                url=upstream_url,
                headers=upstream_headers,
                content=body,
            )
        latency_ms = int((time.perf_counter() - start) * 1000)

        # Extract JTI from JWT for audit
        jti = "unknown"
        try:
            from jose import jwt as jose_jwt
            jti = jose_jwt.get_unverified_claims(jwt).get("jti", "unknown")
        except Exception:
            pass

        await audit.record_event(
            "API_CALL", token_id, pat_ctx["user_sub"], jti,
            trace_id, client_ip, method, capi_path, resp.status_code, latency_ms
        )
        cdp_api_requests_total.labels(token_id=token_id, status_code=resp.status_code).inc()
        cdp_api_latency_seconds.labels(api_code=route["scope"].split(".")[1]).observe(latency_ms / 1000)

        # Build response with quota headers
        response_headers = dict(resp.headers)
        response_headers.update(quota_headers)
        response_headers["X-Request-Id"] = trace_id

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=response_headers,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.TimeoutException:
        return _problem_json("CDP-2002", "Upstream timeout", 504, "Upstream request timed out", capi_path)
    except Exception as e:
        logger.error(f"Upstream error: {e}")
        return _problem_json("CDP-2002", "Upstream error", 502, str(e), capi_path)
