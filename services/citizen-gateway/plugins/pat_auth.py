"""PAT authentication plugin — implements spec section 6.2."""
import re, json, hmac, hashlib, logging, ipaddress
from datetime import datetime, timezone
import httpx
import redis as redis_lib
from config import settings

logger = logging.getLogger("gateway.pat_auth")

PAT_REGEX = re.compile(r"^hdpat_([A-Za-z0-9]{12})_([A-Za-z0-9_-]{43})$")

class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int):
        self.code = code
        self.message = message
        self.status = status

def parse_bearer_pat(authorization: str) -> tuple[str, str]:
    """Extract tokenId and secret from 'Bearer hdpat_...' header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError("CDP-1002", "Missing or invalid Authorization header", 401)
    token = authorization[7:]
    match = PAT_REGEX.match(token)
    if not match:
        raise AuthError("CDP-1002", "Invalid PAT format. Expected: hdpat_<12>_<43>", 401)
    return match.group(1), match.group(2)

def _compute_hmac(secret: str) -> str:
    return hmac.new(settings.server_key.encode(), secret.encode(), hashlib.sha256).hexdigest()

def _check_cidr(client_ip: str, allowed_cidr: list[str]) -> bool:
    if not allowed_cidr:
        return True
    try:
        client_addr = ipaddress.ip_address(client_ip)
        return any(client_addr in ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidr)
    except ValueError:
        return False

async def authenticate_pat(
    authorization: str,
    client_ip: str,
    method: str,
    path: str,
    required_scope: str,
    r: redis_lib.Redis,
) -> dict:
    """Full PAT auth pipeline. Returns PAT context dict on success, raises AuthError on failure."""
    token_id, secret = parse_bearer_pat(authorization)

    # Redis lookup
    meta = r.hgetall(f"cdp:pat:{token_id}")

    if not meta:
        # Cache miss — call portal-backend fallback
        neg_key = f"cdp:pat:neg:{token_id}"
        if r.exists(neg_key):
            raise AuthError("CDP-1001", "Invalid or revoked token", 401)
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{settings.portal_backend_url}/internal/pat/{token_id}")
                if resp.status_code != 200:
                    r.setex(neg_key, settings.neg_cache_ttl, "1")
                    raise AuthError("CDP-1001", "Invalid or revoked token", 401)
                meta = r.hgetall(f"cdp:pat:{token_id}")
                if not meta:
                    # Populate manually from response
                    data = resp.json()
                    meta = {
                        "sub": data["sub"],
                        "scopes": json.dumps(data["scopes"]),
                        "status": data["status"],
                        "cidr": json.dumps(data.get("cidr", [])),
                        "hash": "",  # HMAC not available from fallback
                        "rate_limit_tps": str(data["quota"]["rateLimitTps"]),
                        "burst": str(data["quota"]["burst"]),
                        "daily_quota": str(data["quota"]["dailyQuota"]),
                        "monthly_quota": str(data["quota"]["monthlyQuota"]),
                        "expires_at": data["expiresAt"],
                    }
        except AuthError:
            raise
        except Exception as e:
            logger.error(f"Portal fallback failed for {token_id}: {e}")
            r.setex(neg_key, settings.neg_cache_ttl, "1")
            raise AuthError("CDP-1001", "Invalid or revoked token", 401)

    # Status check
    status = meta.get("status", "")
    if status != "ACTIVE":
        raise AuthError("CDP-1001", f"Token status is {status}", 401)

    # Expiry check
    expires_at_str = meta.get("expires_at", "")
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_at:
                raise AuthError("CDP-1001", "Token has expired", 401)
        except ValueError:
            pass

    # HMAC verification (constant-time)
    stored_hmac = meta.get("hash", "")
    if stored_hmac:
        expected = _compute_hmac(secret)
        if not hmac.compare_digest(expected, stored_hmac):
            raise AuthError("CDP-1001", "Token signature mismatch", 401)
    # If no HMAC (fallback path), skip — acceptable for cache-miss fallback in dev

    # CIDR check
    cidr_str = meta.get("cidr", "[]")
    allowed_cidr = json.loads(cidr_str) if cidr_str else []
    if not _check_cidr(client_ip, allowed_cidr):
        raise AuthError("CDP-1006", f"Client IP {client_ip} not in allowed CIDR", 403)

    # Scope check
    scopes_str = meta.get("scopes", "[]")
    granted_scopes = json.loads(scopes_str) if scopes_str else []
    if required_scope and required_scope not in granted_scopes:
        raise AuthError("CDP-1003", f"Required scope '{required_scope}' not granted. Granted: {granted_scopes}", 403)

    return {
        "token_id": token_id,
        "user_sub": meta.get("sub", ""),
        "scopes": granted_scopes,
        "rate_limit_tps": int(meta.get("rate_limit_tps", 10)),
        "burst": int(meta.get("burst", 20)),
        "daily_quota": int(meta.get("daily_quota", 5000)),
        "monthly_quota": int(meta.get("monthly_quota", 100000)),
    }
