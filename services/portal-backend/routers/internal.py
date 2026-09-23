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
    """Gateway fallback for a Redis cache miss.

    Returns the same HMAC value the gateway would have found in Redis, so
    pat-auth.lua's Fail-Closed check (secret HMAC == meta.hash) still runs.
    Legacy rows without token_hmac cannot be verified this way and are
    treated as invalid — Redis remains the source of truth for the HMAC on
    the hot path, this endpoint just rehydrates it after eviction.
    """
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

    # A row without token_hmac predates the P0 fix and can't be verified by
    # the gateway on cache miss. Reject rather than silently letting an
    # attacker through — issuers must re-issue such PATs.
    if not pat["token_hmac"]:
        raise HTTPException(401, detail={"code": "CDP-1001", "message": "PAT missing verification material; re-issue required"})

    # Re-cache in Redis
    r = rc.get_redis()
    now = datetime.now(timezone.utc)
    expires_at = pat["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    ttl = max(0, int((expires_at - now).total_seconds()))

    if ttl > 0:
        rc.cache_pat_meta(r, token_id, {
            "hmac": pat["token_hmac"],
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
        "hmac": pat["token_hmac"],
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

class UserLifecycleEvent(BaseModel):
    userSub: str
    eventType: str  # e.g. DISABLE, DELETE, LOGOUT
    reason: Optional[str] = None


@router.post("/user-events", status_code=202)
async def receive_user_event(
    event: UserLifecycleEvent,
    db: asyncpg.Connection = Depends(get_db),
):
    """Keycloak lifecycle webhook. DISABLE/DELETE revokes every ACTIVE PAT
    for the user and drops both the PAT and JWT caches immediately, so the
    next gateway request 401s instead of riding on a valid-but-stale
    cached JWT until its 240s TTL expires. LOGOUT is a session-scoped event
    on Keycloak's side and only needs the JWT cache dropped (PATs continue).
    Idempotent — repeat calls are no-ops if nothing is left to revoke."""
    event_type = (event.eventType or "").upper()
    if event_type not in {"DISABLE", "DELETE", "LOGOUT"}:
        raise HTTPException(400, detail={"code": "CDP-4001", "message": f"Unsupported eventType: {event.eventType}"})

    r = rc.get_redis()

    if event_type in {"DISABLE", "DELETE"}:
        rows = await db.fetch(
            "SELECT token_id FROM cdp.pat WHERE user_sub=$1 AND status='ACTIVE'",
            event.userSub,
        )
        token_ids = [row["token_id"] for row in rows]
        if token_ids:
            await db.execute(
                """UPDATE cdp.pat SET status='REVOKED', revoked_at=now(),
                                       revoked_by='keycloak-webhook',
                                       revoke_reason=$2
                   WHERE user_sub=$1 AND status='ACTIVE'""",
                event.userSub, f"user {event_type.lower()}: {event.reason or 'no reason given'}",
            )
            for tid in token_ids:
                rc.invalidate_pat(r, tid)
            await db.executemany(
                """INSERT INTO cdp.audit_log (event_type, token_id, user_sub, detail)
                   VALUES ('PAT_REVOKED', $1, $2, $3::jsonb)""",
                [(tid, event.userSub, json.dumps({"trigger": "keycloak", "event": event_type})) for tid in token_ids],
            )
        return {"processed": len(token_ids), "action": "revoked"}

    # LOGOUT: drop JWT cache only, PATs remain.
    rows = await db.fetch(
        "SELECT token_id FROM cdp.pat WHERE user_sub=$1 AND status='ACTIVE'",
        event.userSub,
    )
    token_ids = [row["token_id"] for row in rows]
    dropped = rc.invalidate_jwt_cache(r, token_ids) if token_ids else 0
    return {"processed": len(token_ids), "action": "jwt-cache-cleared", "keysDeleted": dropped}


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
