"""Audit log endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
import asyncpg
from database import get_db
from auth import get_current_user, UserClaims
from error_handlers import cdp_error

router = APIRouter(prefix="/audit", tags=["audit"])

@router.get("/logs")
async def list_audit_logs(
    token_id: Optional[str] = Query(None),
    user_sub: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    if not any(r in user.roles for r in ["platform-admin", "auditor"]):
        return cdp_error("CDP-4003", "Only auditors or admins can view audit logs", "/audit/logs")

    conditions = ["1=1"]
    values = []
    idx = 1

    if token_id:
        conditions.append(f"token_id=${idx}"); values.append(token_id); idx += 1
    if user_sub:
        conditions.append(f"user_sub=${idx}"); values.append(user_sub); idx += 1
    if event_type:
        conditions.append(f"event_type=${idx}"); values.append(event_type); idx += 1

    where = " AND ".join(conditions)
    offset = (page - 1) * size
    values.extend([size, offset])

    rows = await db.fetch(
        f"""SELECT log_id, occurred_at, event_type, token_id, user_sub, jwt_jti,
                   trace_id, client_ip::text, http_method, request_path, status_code,
                   latency_ms, error_code
            FROM cdp.audit_log WHERE {where}
            ORDER BY occurred_at DESC LIMIT ${idx} OFFSET ${idx+1}""",
        *values
    )
    total = await db.fetchval(f"SELECT COUNT(*) FROM cdp.audit_log WHERE {where}", *values[:-2])
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "size": size,
    }
