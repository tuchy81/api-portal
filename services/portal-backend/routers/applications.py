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
    grantedScopes: Optional[list[str]] = None
    reviewComment: Optional[str] = None

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

    # Eligibility check
    required_roles = api["required_roles"] or []
    user_roles = await _get_user_roles_from_idp(user.sub)
    missing_roles = [r for r in required_roles if r not in user_roles and r not in user.roles]
    eligible = len(missing_roles) == 0

    app_id = uuid.uuid4()
    await db.execute(
        """INSERT INTO cdp.application
           (app_id, api_id, user_sub, user_name, purpose, expected_tps, expected_daily, valid_until, status, granted_scopes)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'PENDING','{}')""",
        app_id, uuid.UUID(req.apiId), user.sub, user.username,
        req.purpose, req.expectedTps, req.expectedDaily,
        date.fromisoformat(req.validUntil)
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

    if is_owner and role == "reviewer":
        rows = await db.fetch(
            """SELECT a.*, c.name as api_name, c.api_code FROM cdp.application a
               JOIN cdp.api_catalog c ON a.api_id=c.api_id
               WHERE c.owner_sub=$1 AND ($2::varchar IS NULL OR a.status=$2)
               ORDER BY a.created_at DESC""",
            user.sub, status
        )
    else:
        rows = await db.fetch(
            """SELECT a.*, c.name as api_name, c.api_code FROM cdp.application a
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
        granted = req.grantedScopes or []
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
