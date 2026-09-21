"""Internal endpoints called by citizen gateway."""
import hmac, json, logging
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
import asyncpg
from database import get_db
import redis_client as rc
import pat_utils
from config import settings
from datetime import datetime, timezone

logger = logging.getLogger("portal.internal")


def require_internal_key(x_internal_key: Optional[str] = Header(None)):
    """These endpoints hand out PAT metadata and accept audit records, so an
    unauthenticated caller could enumerate tokens or forge the audit trail.
    The gateway plugins send this header (see apisix_client._build_route)."""
    if not settings.internal_api_key:
        return
    if not x_internal_key or not hmac.compare_digest(x_internal_key, settings.internal_api_key):
        raise HTTPException(403, detail={"code": "CDP-1005", "message": "Invalid internal API key"})


router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal_key)])

class AuditBatchItem(BaseModel):
    token_id: Optional[str] = None
    user_sub: Optional[str] = None
    jwt_jti: Optional[str] = None
    trace_id: Optional[str] = None
    client_ip: Optional[str] = None
    http_method: Optional[str] = None
    request_path: Optional[str] = None
    status_code: Optional[int] = None
    latency_ms: Optional[int] = None
    error_code: Optional[str] = None
    event_type: str = "API_CALL"

@router.get("/pat/{token_id}")
async def get_pat_meta(
    token_id: str,
    db: asyncpg.Connection = Depends(get_db),
):
    """Gateway fallback: return PAT metadata for cache population."""
    pat = await db.fetchrow(
        """SELECT p.*, q.rate_limit_tps, q.burst, q.daily_quota, q.monthly_quota
           FROM cdp.pat p LEFT JOIN cdp.quota_policy q ON p.token_id=q.token_id
           WHERE p.token_id=$1""",
        token_id
    )
    if not pat:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "PAT not found"})

    if pat["status"] != "ACTIVE":
        raise HTTPException(401, detail={"code": "CDP-1001", "message": "PAT is not active"})

    # Re-cache in Redis
    r = rc.get_redis()
    now = datetime.now(timezone.utc)
    expires_at = pat["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    ttl = max(0, int((expires_at - now).total_seconds()))

    if ttl > 0:
        rc.cache_pat_meta(r, token_id, {
            "hmac": pat["token_hash"],  # Note: DB stores a SHA256 hash, but gateway uses HMAC
            # For gateway to work, we need to store HMAC — but it's not stored in DB
            # The gateway should NOT use this fallback for HMAC verification in production
            # (HMAC is stored only during initial issuance in Redis)
            # This fallback is for metadata only; HMAC comparison will fail if Redis was cleared
            "sub": pat["user_sub"],
            "scopes": list(pat["scopes"]) if pat["scopes"] else [],
            "status": pat["status"],
            "cidr": list(pat["allowed_cidr"]) if pat["allowed_cidr"] else [],
            "rate_limit_tps": pat["rate_limit_tps"] or 10,
            "burst": pat["burst"] or 20,
            "daily_quota": pat["daily_quota"] or 5000,
            "monthly_quota": pat["monthly_quota"] or 100000,
            "expires_at": expires_at.isoformat(),
        }, ttl)

    return {
        "tokenId": token_id,
        "sub": pat["user_sub"],
        "scopes": list(pat["scopes"]) if pat["scopes"] else [],
        "status": pat["status"],
        "cidr": list(pat["allowed_cidr"]) if pat["allowed_cidr"] else [],
        "expiresAt": expires_at.isoformat(),
        "quota": {
            "rateLimitTps": pat["rate_limit_tps"] or 10,
            "burst": pat["burst"] or 20,
            "dailyQuota": pat["daily_quota"] or 5000,
            "monthlyQuota": pat["monthly_quota"] or 100000,
        }
    }

@router.post("/audit", status_code=202)
async def receive_audit_batch(
    batch: list[AuditBatchItem],
    db: asyncpg.Connection = Depends(get_db),
):
    """Receive async audit batch from citizen gateway pat-audit plugin."""
    for item in batch:
        # Mask any PAT plaintext in paths or details
        safe_path = pat_utils.mask_pat(item.request_path or "")

        await db.execute(
            """INSERT INTO cdp.audit_log
               (event_type, token_id, user_sub, jwt_jti, trace_id, client_ip,
                http_method, request_path, status_code, latency_ms, error_code)
               VALUES ($1,$2,$3,$4,$5,$6::inet,$7,$8,$9,$10,$11)""",
            item.event_type, item.token_id, item.user_sub, item.jwt_jti,
            item.trace_id, item.client_ip, item.http_method,
            safe_path, item.status_code, item.latency_ms, item.error_code
        )

        # Update last_used_at
        if item.token_id and item.event_type == "API_CALL":
            await db.execute(
                "UPDATE cdp.pat SET last_used_at=now() WHERE token_id=$1",
                item.token_id
            )
    return {"processed": len(batch)}
