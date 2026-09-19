"""Mock Internal Gateway — validates JWT, simulates PDP (always PERMIT), returns mock data."""
import os, time, logging
from typing import Optional
from functools import lru_cache
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from jose import jwt as jose_jwt, JWTError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mock-internal-gw")

KC_URL = os.getenv("KC_URL", "http://mock-keycloak:8180")
KC_REALM = os.getenv("KC_REALM", "hd")
EXPECTED_ISSUER = f"{KC_URL}/realms/{KC_REALM}"
EXPECTED_AUDIENCE = "internal-api-gateway"

# ---------------------------------------------------------------------------
# JWKS cache (5-min TTL)
# ---------------------------------------------------------------------------
_jwks_cache: dict = {"keys": None, "fetched_at": 0}

async def get_jwks() -> dict:
    now = time.time()
    if _jwks_cache["keys"] is None or now - _jwks_cache["fetched_at"] > 300:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{KC_URL}/realms/{KC_REALM}/protocol/openid-connect/certs", timeout=5)
            resp.raise_for_status()
            _jwks_cache["keys"] = resp.json()
            _jwks_cache["fetched_at"] = now
    return _jwks_cache["keys"]

async def verify_jwt(token: str) -> dict:
    jwks = await get_jwks()
    try:
        claims = jose_jwt.decode(
            token, jwks,
            algorithms=["RS256"],
            audience=EXPECTED_AUDIENCE,
            issuer=EXPECTED_ISSUER,
            options={"verify_exp": True}
        )
        return claims
    except JWTError as e:
        raise HTTPException(403, detail={"code": "CDP-1005", "message": f"JWT validation failed: {e}"})

# ---------------------------------------------------------------------------
# JWT auth middleware
# ---------------------------------------------------------------------------
app = FastAPI(title="Mock Internal Gateway")

@app.middleware("http")
async def jwt_auth_middleware(request: Request, call_next):
    if request.url.path in ["/health", "/internal/api/v1/health"]:
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse({"code": "CDP-1005", "message": "Missing Bearer JWT"}, status_code=403)

    token = auth[7:]
    try:
        claims = await verify_jwt(token)
    except HTTPException as e:
        return JSONResponse(e.detail, status_code=e.status_code)

    # Log citizen headers for audit trail
    pat_id = request.headers.get("X-Citizen-PAT-Id", "-")
    channel = request.headers.get("X-Citizen-Channel", "-")
    req_id = request.headers.get("X-Request-Id", "-")
    logger.info(f"[INTERNAL-GW] sub={claims.get('sub')} pat={pat_id} channel={channel} req_id={req_id} path={request.url.path}")

    request.state.claims = claims
    return await call_next(request)

# ---------------------------------------------------------------------------
# Mock API endpoints
# ---------------------------------------------------------------------------
MOCK_VENDORS = [
    {"id": "v001", "name": "삼성전자㈜", "code": "S001", "status": "ACTIVE", "tier": "A"},
    {"id": "v002", "name": "LG이노텍㈜", "code": "L002", "status": "ACTIVE", "tier": "B"},
    {"id": "v003", "name": "SK하이닉스㈜", "code": "S003", "status": "ACTIVE", "tier": "A"},
    {"id": "v004", "name": "현대모비스㈜", "code": "H004", "status": "INACTIVE", "tier": "C"},
    {"id": "v005", "name": "포스코홀딩스㈜", "code": "P005", "status": "ACTIVE", "tier": "A"},
]

MOCK_ORDERS = [
    {"id": "o001", "vendor_id": "v001", "amount": 15000000, "status": "CONFIRMED", "date": "2026-09-01"},
    {"id": "o002", "vendor_id": "v002", "amount": 7500000,  "status": "PENDING",   "date": "2026-09-10"},
    {"id": "o003", "vendor_id": "v003", "amount": 32000000, "status": "CONFIRMED", "date": "2026-09-15"},
]

MOCK_EMPLOYEES = [
    {"id": "e001", "name": "김철수", "dept": "IT기획팀", "position": "팀장"},
    {"id": "e002", "name": "이영희", "dept": "MDM운영팀", "position": "선임"},
    {"id": "e003", "name": "박민준", "dept": "영업관리팀", "position": "대리"},
]

@app.get("/internal/api/v1/vendors")
async def list_vendors(request: Request, page: int = 1, size: int = 50):
    return {"items": MOCK_VENDORS, "total": len(MOCK_VENDORS), "page": page, "size": size}

@app.get("/internal/api/v1/vendors/{vendor_id}")
async def get_vendor(vendor_id: str, request: Request):
    vendor = next((v for v in MOCK_VENDORS if v["id"] == vendor_id), None)
    if not vendor:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": f"Vendor {vendor_id} not found"})
    return vendor

@app.post("/internal/api/v1/vendors")
async def create_vendor(request: Request):
    body = await request.json()
    body["id"] = f"v{len(MOCK_VENDORS)+1:03d}"
    return JSONResponse(body, status_code=201)

@app.get("/internal/api/v1/orders")
async def list_orders(request: Request, page: int = 1, size: int = 50):
    return {"items": MOCK_ORDERS, "total": len(MOCK_ORDERS), "page": page, "size": size}

@app.get("/internal/api/v1/orders/{order_id}")
async def get_order(order_id: str, request: Request):
    order = next((o for o in MOCK_ORDERS if o["id"] == order_id), None)
    if not order:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": f"Order {order_id} not found"})
    return order

@app.get("/internal/api/v1/employees")
async def list_employees(request: Request):
    # Check cdp_channel and hr-reader scope
    claims = getattr(request.state, "claims", {})
    scope = claims.get("scope", "")
    if "capi.hr.read" not in scope and "hr-reader" not in str(claims.get("realm_access", {}).get("roles", [])):
        return JSONResponse({"code": "CDP-1005", "message": "Insufficient scope for HR data"}, status_code=403)
    return {"items": MOCK_EMPLOYEES, "total": len(MOCK_EMPLOYEES)}

@app.get("/internal/api/v1/health")
@app.get("/health")
async def health():
    return {"status": "ok", "service": "mock-internal-gw"}
