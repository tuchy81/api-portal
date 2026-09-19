"""API Catalog endpoints."""
import uuid, logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import asyncpg
import httpx
from database import get_db
from auth import get_current_user, UserClaims
from error_handlers import cdp_error
from config import settings

logger = logging.getLogger("portal.catalog")
router = APIRouter(prefix="/catalog/apis", tags=["catalog"])

class ApiCreateRequest(BaseModel):
    apiCode: str
    name: str
    description: Optional[str] = None
    ownerDept: str
    upstreamUrl: str
    publicPath: str
    requiredRoles: list[str] = []
    scopes: list[dict] = []  # [{scopeName, httpMethod, pathPattern, description}]

class ApiPatchRequest(BaseModel):
    status: Optional[str] = None
    description: Optional[str] = None
    name: Optional[str] = None

@router.get("")
async def list_apis(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    q: Optional[str] = None,
    status: str = "PUBLISHED",
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    offset = (page - 1) * size
    if q:
        rows = await db.fetch(
            """SELECT api_id, api_code, name, description, owner_dept, public_path, status, required_roles, created_at
               FROM cdp.api_catalog
               WHERE status=$1 AND (name ILIKE $2 OR description ILIKE $2 OR api_code ILIKE $2)
               ORDER BY created_at DESC LIMIT $3 OFFSET $4""",
            status, f"%{q}%", size, offset
        )
    else:
        rows = await db.fetch(
            """SELECT api_id, api_code, name, description, owner_dept, public_path, status, required_roles, created_at
               FROM cdp.api_catalog WHERE status=$1
               ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
            status, size, offset
        )
    total = await db.fetchval("SELECT COUNT(*) FROM cdp.api_catalog WHERE status=$1", status)
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "size": size,
    }

@router.get("/{api_id}")
async def get_api(
    api_id: str,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    row = await db.fetchrow(
        "SELECT * FROM cdp.api_catalog WHERE api_id=$1", uuid.UUID(api_id)
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "API not found"})
    api = dict(row)
    scopes = await db.fetch("SELECT * FROM cdp.api_scope WHERE api_id=$1", uuid.UUID(api_id))
    api["scopes"] = [dict(s) for s in scopes]
    return api

@router.post("", status_code=201)
async def create_api(
    req: ApiCreateRequest,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    if not any(r in user.roles for r in ["api-owner", "platform-admin"]):
        return cdp_error("CDP-4003", "Only API owners or admins can register APIs", f"/catalog/apis")

    api_id = uuid.uuid4()
    async with db.transaction():
        await db.execute(
            """INSERT INTO cdp.api_catalog
               (api_id, api_code, name, description, owner_dept, owner_sub, upstream_url, public_path, required_roles, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'PUBLISHED')""",
            api_id, req.apiCode, req.name, req.description,
            req.ownerDept, user.sub, req.upstreamUrl, req.publicPath,
            req.requiredRoles
        )
        for scope in req.scopes:
            await db.execute(
                """INSERT INTO cdp.api_scope (api_id, scope_name, http_method, path_pattern, description)
                   VALUES ($1,$2,$3,$4,$5)""",
                api_id, scope["scopeName"], scope["httpMethod"],
                scope["pathPattern"], scope.get("description")
            )
    logger.info(f"API {req.apiCode} published by {user.sub}")
    return {"apiId": str(api_id), "apiCode": req.apiCode, "status": "PUBLISHED"}

@router.patch("/{api_id}")
async def patch_api(
    api_id: str,
    req: ApiPatchRequest,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    if not any(r in user.roles for r in ["api-owner", "platform-admin"]):
        return cdp_error("CDP-4003", "Only API owners or admins can modify APIs", f"/catalog/apis/{api_id}")

    updates = []
    values = []
    idx = 1
    if req.status:
        updates.append(f"status=${idx}"); values.append(req.status); idx += 1
    if req.name:
        updates.append(f"name=${idx}"); values.append(req.name); idx += 1
    if req.description is not None:
        updates.append(f"description=${idx}"); values.append(req.description); idx += 1
    if not updates:
        raise HTTPException(400, detail="No fields to update")

    updates.append(f"updated_at=now()")
    values.append(uuid.UUID(api_id))
    await db.execute(
        f"UPDATE cdp.api_catalog SET {', '.join(updates)} WHERE api_id=${idx}",
        *values
    )
    return {"apiId": api_id, "updated": True}
