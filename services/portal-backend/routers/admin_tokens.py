"""Platform-admin PAT oversight — expiry review and bulk revocation (UC-10, R-12).

R-12's original form was "PAT 만료 사전 알림". There is no notification channel
in this system, so the requirement is met here as a review screen instead: an
admin can list what is about to expire (and who owns it) and act on it.
"""
import json, logging
from typing import Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
import asyncpg
from database import get_db
from auth import get_current_user, UserClaims
from error_handlers import cdp_error
import redis_client as rc

logger = logging.getLogger("portal.admin_tokens")
router = APIRouter(prefix="/admin/tokens", tags=["admin"])


class BulkRevokeRequest(BaseModel):
    tokenIds: list[str] = []
    userSubs: list[str] = []
    expiredOnly: bool = False
    reason: Optional[str] = None


def _require_admin(user: UserClaims, instance: str):
    if "platform-admin" not in user.roles:
        return cdp_error("CDP-4003", "Only platform admins can manage all PATs", instance)
    return None


@router.get("")
async def list_all_tokens(
    expiringInDays: Optional[int] = Query(None, ge=0, le=365),
    includeExpired: bool = Query(False),
    userSubs: Optional[str] = Query(None, description="comma-separated user ids"),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    denied = _require_admin(user, "/admin/tokens")
    if denied:
        return denied

    conditions = ["1=1"]
    values = []
    idx = 1

    if expiringInDays is not None:
        conditions.append(f"p.expires_at <= now() + (${idx} || ' days')::interval")
        values.append(str(expiringInDays)); idx += 1
    if not includeExpired:
        conditions.append("p.expires_at > now()")
    if status:
        conditions.append(f"p.status = ${idx}"); values.append(status); idx += 1
    else:
        # An already-revoked PAT isn't "expiring" in any useful sense, and on a
        # long-lived system it's the bulk of the table. Ask for it explicitly.
        conditions.append("p.status <> 'REVOKED'")
    if userSubs:
        subs = [s.strip() for s in userSubs.replace("\n", ",").split(",") if s.strip()]
        if subs:
            conditions.append(f"p.user_sub = ANY(${idx}::text[])"); values.append(subs); idx += 1

    where = " AND ".join(conditions)
    offset = (page - 1) * size

    rows = await db.fetch(
        f"""SELECT p.token_id, p.token_name, p.user_sub, a.user_name,
                   c.api_code, c.name AS api_name, p.status,
                   p.issued_at, p.expires_at, p.last_used_at,
                   EXTRACT(DAY FROM (p.expires_at - now()))::int AS days_until_expiry
            FROM cdp.pat p
            JOIN cdp.application a ON p.app_id = a.app_id
            JOIN cdp.api_catalog c ON a.api_id = c.api_id
            WHERE {where}
            ORDER BY p.expires_at ASC
            LIMIT ${idx} OFFSET ${idx + 1}""",
        *values, size, offset
    )
    total = await db.fetchval(
        f"""SELECT COUNT(*) FROM cdp.pat p
            JOIN cdp.application a ON p.app_id = a.app_id
            JOIN cdp.api_catalog c ON a.api_id = c.api_id
            WHERE {where}""",
        *values
    )

    return {
        "items": [
            {
                "tokenId": r["token_id"],
                "tokenName": r["token_name"],
                "userSub": r["user_sub"],
                "userName": r["user_name"],
                "apiCode": r["api_code"],
                "apiName": r["api_name"],
                "status": r["status"],
                "issuedAt": r["issued_at"],
                "expiresAt": r["expires_at"],
                "lastUsedAt": r["last_used_at"],
                "daysUntilExpiry": r["days_until_expiry"],
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.post("/revoke")
async def bulk_revoke(
    req: BulkRevokeRequest,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    denied = _require_admin(user, "/admin/tokens/revoke")
    if denied:
        return denied

    if not req.tokenIds and not req.userSubs and not req.expiredOnly:
        return cdp_error("CDP-4001",
            "Specify at least one of tokenIds, userSubs or expiredOnly",
            "/admin/tokens/revoke")

    # Union of the selectors, restricted to tokens that are still live —
    # re-revoking an already revoked PAT would just add noise to the audit log.
    targets: set[str] = set()
    if req.tokenIds:
        rows = await db.fetch(
            "SELECT token_id FROM cdp.pat WHERE token_id = ANY($1::text[]) AND status <> 'REVOKED'",
            req.tokenIds,
        )
        targets.update(r["token_id"] for r in rows)
    if req.userSubs:
        rows = await db.fetch(
            "SELECT token_id FROM cdp.pat WHERE user_sub = ANY($1::text[]) AND status <> 'REVOKED'",
            req.userSubs,
        )
        targets.update(r["token_id"] for r in rows)
    if req.expiredOnly:
        rows = await db.fetch(
            "SELECT token_id FROM cdp.pat WHERE expires_at < now() AND status <> 'REVOKED'"
        )
        targets.update(r["token_id"] for r in rows)

    if not targets:
        return {"revoked": 0, "tokenIds": []}

    token_ids = sorted(targets)
    reason = req.reason or "bulk revocation by admin"

    await db.execute(
        """UPDATE cdp.pat
           SET status='REVOKED', revoked_at=now(), revoked_by=$1, revoke_reason=$2
           WHERE token_id = ANY($3::text[])""",
        user.sub, reason, token_ids,
    )

    r = rc.get_redis()
    for token_id in token_ids:
        rc.invalidate_pat(r, token_id)

    detail = json.dumps({"bulk": True, "reason": reason})
    await db.executemany(
        """INSERT INTO cdp.audit_log (event_type, token_id, user_sub, detail)
           VALUES ('PAT_REVOKED', $1, $2, $3::jsonb)""",
        [(tid, user.sub, detail) for tid in token_ids],
    )

    logger.info(f"bulk revoke by {user.sub}: {len(token_ids)} PATs ({reason})")
    return {"revoked": len(token_ids), "tokenIds": token_ids}
