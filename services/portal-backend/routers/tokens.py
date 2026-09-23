"""PAT lifecycle endpoints."""
import uuid, logging
from datetime import datetime, timezone, timedelta, time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import asyncpg
from database import get_db
from auth import get_current_user, UserClaims
from error_handlers import cdp_error
import pat_utils
import redis_client as rc
from config import settings

logger = logging.getLogger("portal.tokens")
router = APIRouter(prefix="/tokens", tags=["tokens"])

class TokenIssueRequest(BaseModel):
    appId: str
    tokenName: str
    validDays: int = 90
    allowedCidr: list[str] = []

class QuotaPatchRequest(BaseModel):
    rateLimitTps: Optional[int] = None
    burst: Optional[int] = None
    dailyQuota: Optional[int] = None
    monthlyQuota: Optional[int] = None
    concurrency: Optional[int] = None

@router.post("", status_code=201)
async def issue_pat(
    req: TokenIssueRequest,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    # Validate validDays
    if not (1 <= req.validDays <= settings.pat_max_days):
        return cdp_error("CDP-4001", f"validDays must be between 1 and {settings.pat_max_days}", "/tokens")

    # Load approved application
    app = await db.fetchrow(
        """SELECT a.*, c.api_id as catalog_api_id FROM cdp.application a
           JOIN cdp.api_catalog c ON a.api_id=c.api_id
           WHERE a.app_id=$1 AND a.status='APPROVED'""",
        uuid.UUID(req.appId)
    )
    if not app or app["user_sub"] != user.sub:
        return cdp_error("CDP-4003", "Application not approved or not owned by you", "/tokens")

    # The application's valid_until (usage period end date, set at application
    # time and reviewed by the approver) was never enforced here before — a
    # citizen could request validDays up to pat_max_days regardless of what
    # was actually approved. Cap the PAT's expiry at end-of-day valid_until so
    # a PAT can never outlive the approved usage window.
    valid_until_end = datetime.combine(app["valid_until"], time.max, tzinfo=timezone.utc)
    if valid_until_end <= datetime.now(timezone.utc):
        return cdp_error(
            "CDP-4009",
            f"Application's approved usage period ended on {app['valid_until']}; "
            "request a new application before issuing a PAT",
            "/tokens",
        )

    # Check PAT limits
    active_per_app = await db.fetchval(
        "SELECT COUNT(*) FROM cdp.pat WHERE app_id=$1 AND status='ACTIVE'",
        uuid.UUID(req.appId)
    )
    if active_per_app >= settings.pat_max_per_app:
        return cdp_error("CDP-4009",
            f"Active PAT limit ({settings.pat_max_per_app}) per application exceeded", "/tokens")

    active_per_user = await db.fetchval(
        "SELECT COUNT(*) FROM cdp.pat WHERE user_sub=$1 AND status='ACTIVE'",
        user.sub
    )
    if active_per_user >= settings.pat_max_per_user:
        return cdp_error("CDP-4009",
            f"Active PAT limit ({settings.pat_max_per_user}) per user exceeded", "/tokens")

    # Generate PAT
    token_id = pat_utils.generate_token_id()
    secret = pat_utils.generate_secret()
    full_token = pat_utils.build_pat(token_id, secret)
    token_hash = pat_utils.hash_secret_sha256(secret)
    hmac_val = pat_utils.compute_hmac(secret, settings.server_key)

    granted_scopes = app["granted_scopes"] or []
    now = datetime.now(timezone.utc)
    requested_expires_at = now + timedelta(days=req.validDays)
    expires_at = min(requested_expires_at, valid_until_end)
    capped_by_valid_until = expires_at < requested_expires_at
    ttl_seconds = int((expires_at - now).total_seconds())

    async with db.transaction():
        await db.execute(
            """INSERT INTO cdp.pat (token_id, app_id, user_sub, token_name, token_hash, scopes,
                                    allowed_cidr, issued_at, expires_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            token_id, uuid.UUID(req.appId), user.sub, req.tokenName,
            token_hash, granted_scopes, req.allowedCidr, now, expires_at
        )
        await db.execute(
            """INSERT INTO cdp.quota_policy (token_id, rate_limit_tps, burst, daily_quota, monthly_quota)
               VALUES ($1,$2,$3,$4,$5)""",
            token_id,
            settings.default_rate_limit_tps, settings.default_burst,
            settings.default_daily_quota, settings.default_monthly_quota
        )

    # Cache in Redis
    r = rc.get_redis()
    rc.cache_pat_meta(r, token_id, {
        "hmac": hmac_val,
        "sub": user.sub,
        "scopes": granted_scopes,
        "status": "ACTIVE",
        "cidr": req.allowedCidr,
        "rate_limit_tps": settings.default_rate_limit_tps,
        "burst": settings.default_burst,
        "daily_quota": settings.default_daily_quota,
        "monthly_quota": settings.default_monthly_quota,
        "expires_at": expires_at.isoformat(),
    }, ttl_seconds)

    # Audit log
    await db.execute(
        """INSERT INTO cdp.audit_log (event_type, token_id, user_sub, detail)
           VALUES ('PAT_ISSUED', $1, $2, $3::jsonb)""",
        token_id, user.sub, f'{{"app_id":"{req.appId}","token_name":"{req.tokenName}"}}'
    )

    logger.info(f"PAT {token_id} issued for user {user.sub}, app {req.appId}")
    if capped_by_valid_until:
        logger.info(
            f"PAT {token_id}: requested validDays={req.validDays} capped to "
            f"application {req.appId}'s valid_until={app['valid_until']}"
        )

    result = {
        "tokenId": token_id,
        "token": full_token,
        "tokenName": req.tokenName,
        "scopes": granted_scopes,
        "quota": {
            "rateLimitTps": settings.default_rate_limit_tps,
            "dailyQuota": settings.default_daily_quota,
            "monthlyQuota": settings.default_monthly_quota,
        },
        "issuedAt": now.isoformat(),
        "expiresAt": expires_at.isoformat(),
        "warning": "토큰 평문은 본 응답에서만 확인 가능합니다. 재조회할 수 없습니다.",
    }
    if capped_by_valid_until:
        result["validityWarning"] = (
            f"신청 승인 시 사용기한({app['valid_until']})이 요청한 유효기간보다 짧아, "
            f"만료일이 {expires_at.date().isoformat()}로 단축되었습니다."
        )
    return result

@router.get("")
async def list_pats(
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    rows = await db.fetch(
        """SELECT p.token_id, p.token_name, p.scopes, p.status, p.issued_at, p.expires_at,
                  p.last_used_at, p.allowed_cidr, q.rate_limit_tps, q.daily_quota, q.monthly_quota,
                  a.app_id, c.name as api_name
           FROM cdp.pat p
           JOIN cdp.application a ON p.app_id=a.app_id
           JOIN cdp.api_catalog c ON a.api_id=c.api_id
           LEFT JOIN cdp.quota_policy q ON p.token_id=q.token_id
           WHERE p.user_sub=$1 ORDER BY p.issued_at DESC""",
        user.sub
    )
    return {"items": [dict(r) for r in rows]}

@router.delete("/{token_id}", status_code=204)
async def revoke_pat(
    token_id: str,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    pat = await db.fetchrow("SELECT * FROM cdp.pat WHERE token_id=$1", token_id)
    if not pat:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "PAT not found"})

    is_admin = any(r in user.roles for r in ["platform-admin"])
    if pat["user_sub"] != user.sub and not is_admin:
        return cdp_error("CDP-4003", "Cannot revoke another user's PAT", f"/tokens/{token_id}")

    await db.execute(
        """UPDATE cdp.pat SET status='REVOKED', revoked_at=now(), revoked_by=$1
           WHERE token_id=$2""",
        user.sub, token_id
    )

    r = rc.get_redis()
    rc.invalidate_pat(r, token_id)

    await db.execute(
        """INSERT INTO cdp.audit_log (event_type, token_id, user_sub)
           VALUES ('PAT_REVOKED', $1, $2)""",
        token_id, user.sub
    )
    logger.info(f"PAT {token_id} revoked by {user.sub}")

@router.put("/{token_id}/quota")
async def update_quota(
    token_id: str,
    req: QuotaPatchRequest,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    if "platform-admin" not in user.roles:
        return cdp_error("CDP-4003", "Only platform admins can change quota", f"/tokens/{token_id}/quota")

    pat = await db.fetchrow("SELECT * FROM cdp.pat WHERE token_id=$1", token_id)
    if not pat:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "PAT not found"})

    updates = []
    values = []
    idx = 1
    for field, val in [
        ("rate_limit_tps", req.rateLimitTps),
        ("burst", req.burst),
        ("daily_quota", req.dailyQuota),
        ("monthly_quota", req.monthlyQuota),
        ("concurrency", req.concurrency),
    ]:
        if val is not None:
            updates.append(f"{field}=${idx}"); values.append(val); idx += 1

    if updates:
        updates.append(f"updated_at=now(), updated_by=${idx}")
        values.extend([user.sub, token_id])
        await db.execute(
            f"UPDATE cdp.quota_policy SET {', '.join(updates)} WHERE token_id=${idx+1}",
            *values
        )

    # Update Redis immediately (no restart needed)
    r = rc.get_redis()
    rc.update_pat_quota_in_redis(r, token_id, req.model_dump(exclude_none=True))

    return {"tokenId": token_id, "updated": True, "quota": req.model_dump(exclude_none=True)}
