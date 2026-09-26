"""Application workflow endpoints."""
import uuid, logging
from typing import Optional
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import asyncpg
import httpx
from database import get_db
from auth import get_current_user, UserClaims
from error_handlers import cdp_error
from config import settings

logger = logging.getLogger("portal.applications")
router = APIRouter(prefix="/applications", tags=["applications"])

class ApplicationCreateRequest(BaseModel):
    apiId: str
    purpose: str
    requestedScopes: list[str]
    expectedTps: int = 1
    expectedDaily: int = 1000
    validUntil: str  # YYYY-MM-DD

class ApplicationPatchRequest(BaseModel):
    action: str  # APPROVE or REJECT
    reviewComment: Optional[str] = None
    # No grantedScopes here on purpose (R-08): the approver decides whether to
    # approve, not which scopes get granted. The granted set is derived on the
    # server from the applicant's request ∩ the catalog's declared scopes, so a
    # reviewer can't widen it or invent a scope string that was never published.

async def _get_user_roles_from_idp(user_sub: str) -> list[str]:
    """Fetch user roles from mock Keycloak."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{settings.kc_url}/admin/realms/{settings.kc_realm}/users/{user_sub}")
            if resp.status_code == 200:
                return resp.json().get("realmRoles", [])
    except Exception as e:
        logger.warning(f"Could not fetch roles for {user_sub}: {e}")
    return []

@router.post("", status_code=201)
async def create_application(
    req: ApplicationCreateRequest,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    # Load API catalog
    api = await db.fetchrow(
        "SELECT * FROM cdp.api_catalog WHERE api_id=$1 AND status='PUBLISHED'",
        uuid.UUID(req.apiId)
    )
    if not api:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "API not found or not published"})

    # Requested scopes must exist in the catalog — this is the upstream half of
    # R-08; the approval path then grants a subset of what was requested.
    catalog_scopes = {
        r["scope_name"]
        for r in await db.fetch("SELECT scope_name FROM cdp.api_scope WHERE api_id=$1", uuid.UUID(req.apiId))
    }
    if not req.requestedScopes:
        return cdp_error("CDP-4001", "At least one scope must be requested", "/applications")
    unknown = [s for s in req.requestedScopes if s not in catalog_scopes]
    if unknown:
        return cdp_error("CDP-4001",
            f"Scopes not defined for this API: {', '.join(unknown)}", "/applications")

    # Eligibility check
    required_roles = api["required_roles"] or []
    user_roles = await _get_user_roles_from_idp(user.sub)
    missing_roles = [r for r in required_roles if r not in user_roles and r not in user.roles]
    eligible = len(missing_roles) == 0

    app_id = uuid.uuid4()
    await db.execute(
        """INSERT INTO cdp.application
           (app_id, api_id, user_sub, user_name, purpose, expected_tps, expected_daily, valid_until, status, requested_scopes, granted_scopes)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'PENDING',$9,'{}')""",
        app_id, uuid.UUID(req.apiId), user.sub, user.username,
        req.purpose, req.expectedTps, req.expectedDaily,
        date.fromisoformat(req.validUntil), req.requestedScopes
    )

    # Reviewer lookup
    reviewers = [{"sub": api["owner_sub"], "dept": api["owner_dept"]}]
    logger.info(f"Application {app_id} created by {user.sub} for API {req.apiId}")

    return {
        "appId": str(app_id),
        "status": "PENDING",
        "eligibility": {"checked": True, "missingRoles": missing_roles, "eligible": eligible},
        "reviewers": reviewers,
    }

@router.get("")
async def list_applications(
    role: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    is_owner = any(r in user.roles for r in ["api-owner", "platform-admin"])

    # public_path/description are joined in so the PAT issue dialog can show
    # which API a request is actually for — the app_id alone is unreadable.
    if is_owner and role == "reviewer":
        rows = await db.fetch(
            """SELECT a.*, c.name as api_name, c.api_code, c.public_path, c.description as api_description
               FROM cdp.application a
               JOIN cdp.api_catalog c ON a.api_id=c.api_id
               WHERE c.owner_sub=$1 AND ($2::varchar IS NULL OR a.status=$2)
               ORDER BY a.created_at DESC""",
            user.sub, status
        )
    else:
        rows = await db.fetch(
            """SELECT a.*, c.name as api_name, c.api_code, c.public_path, c.description as api_description
               FROM cdp.application a
               JOIN cdp.api_catalog c ON a.api_id=c.api_id
               WHERE a.user_sub=$1 AND ($2::varchar IS NULL OR a.status=$2)
               ORDER BY a.created_at DESC""",
            user.sub, status
        )
    return {"items": [dict(r) for r in rows]}

@router.patch("/{app_id}")
async def patch_application(
    app_id: str,
    req: ApplicationPatchRequest,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    if not any(r in user.roles for r in ["api-owner", "platform-admin"]):
        return cdp_error("CDP-4003", "Only API owners or admins can approve/reject applications")

    app = await db.fetchrow("SELECT * FROM cdp.application WHERE app_id=$1", uuid.UUID(app_id))
    if not app:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Application not found"})
    if app["status"] != "PENDING":
        return cdp_error("CDP-4009", f"Application is already {app['status']}", f"/applications/{app_id}")

    if req.action == "APPROVE":
        # Derived, not supplied: requested ∩ currently-published catalog scopes.
        # Re-checking the catalog here matters because scopes can be removed
        # between application and approval.
        catalog_scopes = {
            r["scope_name"]
            for r in await db.fetch("SELECT scope_name FROM cdp.api_scope WHERE api_id=$1", app["api_id"])
        }
        granted = [s for s in (app["requested_scopes"] or []) if s in catalog_scopes]
        if not granted:
            return cdp_error("CDP-4001",
                "No requested scope is still published for this API; ask the applicant to re-apply",
                f"/applications/{app_id}")
        await db.execute(
            """UPDATE cdp.application
               SET status='APPROVED', granted_scopes=$1, reviewer_sub=$2, review_comment=$3, reviewed_at=now()
               WHERE app_id=$4""",
            granted, user.sub, req.reviewComment, uuid.UUID(app_id)
        )
        logger.info(f"Application {app_id} APPROVED by {user.sub}, scopes={granted}")
        return {"appId": app_id, "status": "APPROVED", "grantedScopes": granted}

    elif req.action == "REJECT":
        await db.execute(
            """UPDATE cdp.application
               SET status='REJECTED', reviewer_sub=$1, review_comment=$2, reviewed_at=now()
               WHERE app_id=$3""",
            user.sub, req.reviewComment, uuid.UUID(app_id)
        )
        return {"appId": app_id, "status": "REJECTED"}

    else:
        raise HTTPException(400, detail={"code": "CDP-4001", "message": "Invalid action. Use APPROVE or REJECT"})
