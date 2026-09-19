"""Token exchange plugin — implements spec section 6.4."""
import hashlib, logging
import httpx
import redis as redis_lib
from config import settings

logger = logging.getLogger("gateway.token_exchange")

HEADERS_TO_STRIP = {"x-citizen-pat-id", "x-citizen-channel", "x-forwarded-user", "x-user-sub", "cookie"}

def _scope_hash(scopes: list[str]) -> str:
    return hashlib.sha256(" ".join(sorted(scopes)).encode()).hexdigest()[:8]

async def get_jwt(pat_ctx: dict, r: redis_lib.Redis) -> str:
    """Get JWT for this PAT (from cache or TXS). Returns JWT string."""
    token_id = pat_ctx["token_id"]
    scopes = pat_ctx["scopes"]
    sh = _scope_hash(scopes)
    cache_key = f"cdp:jwt:{token_id}:{sh}"

    # Check cache
    jwt = r.get(cache_key)
    if jwt:
        return jwt

    # Call TXS
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.post(
                f"{settings.txs_url}/internal/token-exchange",
                json={
                    "tokenId": token_id,
                    "userSub": pat_ctx["user_sub"],
                    "scopes": scopes,
                }
            )
            if resp.status_code != 200:
                raise ValueError(f"TXS error {resp.status_code}: {resp.text}")
            return resp.json()["accessToken"]
    except Exception as e:
        logger.error(f"Token exchange failed for {token_id}: {e}")
        raise

def build_upstream_headers(
    original_headers: dict,
    jwt: str,
    pat_ctx: dict,
    trace_id: str,
) -> dict:
    """Build clean headers for upstream (JWT swapped, citizen headers added, risky headers stripped)."""
    headers = {}
    for k, v in original_headers.items():
        if k.lower() not in HEADERS_TO_STRIP and k.lower() not in {"authorization"}:
            headers[k] = v

    headers["Authorization"] = f"Bearer {jwt}"
    headers["X-Citizen-PAT-Id"] = pat_ctx["token_id"]
    headers["X-Citizen-Channel"] = "citizen"
    headers["X-Request-Id"] = trace_id
    return headers
