"""API Catalog endpoints."""
import json, uuid, logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import asyncpg
import httpx
from database import get_db
from auth import get_current_user, UserClaims
from error_handlers import cdp_error
from config import settings
import apisix_client

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
    openapiSpec: Optional[dict] = None

class ApiPatchRequest(BaseModel):
    status: Optional[str] = None
    description: Optional[str] = None
    name: Optional[str] = None
    openapiSpec: Optional[dict] = None

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
    # asyncpg hands back jsonb as a string; the UI expects the parsed object.
    if isinstance(api.get("openapi_spec"), str):
        api["openapi_spec"] = json.loads(api["openapi_spec"])
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

    # Pipeline (spec section 8): DB INSERT as DRAFT -> APISIX Admin API route
    # + plugin chain -> check it actually took -> PUBLISHED. If the gateway
    # check fails, we don't hard-fail the whole request (the catalog entry
    # itself is fine) — we save it as DRAFT and surface a warning so the
    # owner knows to retry (PATCH {"status": "PUBLISHED"} once fixed, or it
    # self-heals on the next portal-backend startup resync).
    api_id = uuid.uuid4()
    async with db.transaction():
        await db.execute(
            """INSERT INTO cdp.api_catalog
               (api_id, api_code, name, description, owner_dept, owner_sub, upstream_url, public_path, required_roles, openapi_spec, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,'DRAFT')""",
            api_id, req.apiCode, req.name, req.description,
            req.ownerDept, user.sub, req.upstreamUrl, req.publicPath,
            req.requiredRoles,
            json.dumps(req.openapiSpec) if req.openapiSpec is not None else None
        )
        for scope in req.scopes:
            await db.execute(
                """INSERT INTO cdp.api_scope (api_id, scope_name, http_method, path_pattern, description)
                   VALUES ($1,$2,$3,$4,$5)""",
                api_id, scope["scopeName"], scope["httpMethod"],
                scope["pathPattern"], scope.get("description")
            )

    api_row = {"api_code": req.apiCode, "upstream_url": req.upstreamUrl, "public_path": req.publicPath}
    scope_rows = [{"scope_name": s["scopeName"], "http_method": s["httpMethod"]} for s in req.scopes]

    status = "DRAFT"
    warning = None
    try:
        await apisix_client.upsert_route(api_row, scope_rows)
        if not await apisix_client.smoke_test(req.publicPath):
            raise RuntimeError("smoke test did not get the expected 401 from pat-auth")
        status = "PUBLISHED"
    except Exception as e:
        logger.warning(f"gateway route registration check failed for {req.apiCode}: {e}")
        warning = "API Gateway(APISIX)에 등록되지 않았습니다. 추후 다시 등록해 주세요."

    await db.execute(
        "UPDATE cdp.api_catalog SET status=$1, updated_at=now() WHERE api_id=$2", status, api_id
    )

    if status == "PUBLISHED":
        logger.info(f"API {req.apiCode} published by {user.sub} (route auto-provisioned in APISIX)")
    else:
        logger.info(f"API {req.apiCode} saved as DRAFT by {user.sub} (gateway not registered)")

    result = {"apiId": str(api_id), "apiCode": req.apiCode, "status": status}
    if warning:
        result["warning"] = warning
    return result

@router.patch("/{api_id}")
async def patch_api(
    api_id: str,
    req: ApiPatchRequest,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    if not any(r in user.roles for r in ["api-owner", "platform-admin"]):
        return cdp_error("CDP-4003", "Only API owners or admins can modify APIs", f"/catalog/apis/{api_id}")

    row = await db.fetchrow("SELECT * FROM cdp.api_catalog WHERE api_id=$1", uuid.UUID(api_id))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "API not found"})

    updates = []
    values = []
    idx = 1
    if req.status:
        updates.append(f"status=${idx}"); values.append(req.status); idx += 1
    if req.name:
        updates.append(f"name=${idx}"); values.append(req.name); idx += 1
    if req.description is not None:
        updates.append(f"description=${idx}"); values.append(req.description); idx += 1
    if req.openapiSpec is not None:
        updates.append(f"openapi_spec=${idx}::jsonb"); values.append(json.dumps(req.openapiSpec)); idx += 1
    if not updates:
        raise HTTPException(400, detail="No fields to update")

    updates.append(f"updated_at=now()")
    values.append(uuid.UUID(api_id))
    await db.execute(
        f"UPDATE cdp.api_catalog SET {', '.join(updates)} WHERE api_id=${idx}",
        *values
    )

    # Keep the gateway route in sync with a status change. Best-effort: the
    # DB status is the source of truth either way, and a failed sync here
    # self-heals on the next portal-backend startup (see main.py) or the
    # next successful PATCH.
    warning = None
    if req.status and req.status != row["status"]:
        try:
            if req.status == "PUBLISHED":
                scopes = await db.fetch("SELECT * FROM cdp.api_scope WHERE api_id=$1", uuid.UUID(api_id))
                api_row = {"api_code": row["api_code"], "upstream_url": row["upstream_url"], "public_path": row["public_path"]}
                scope_rows = [{"scope_name": s["scope_name"], "http_method": s["http_method"]} for s in scopes]
                await apisix_client.upsert_route(api_row, scope_rows)
                if not await apisix_client.smoke_test(row["public_path"]):
                    raise RuntimeError("smoke test did not get the expected 401 from pat-auth")
            else:
                await apisix_client.delete_route(row["api_code"], row["public_path"])
        except Exception as e:
            logger.warning(f"gateway route sync failed for {row['api_code']} ({req.status}): {e}")
            warning = "API Gateway(APISIX)에 등록되지 않았습니다. 추후 다시 등록해 주세요."

    result = {"apiId": api_id, "updated": True}
    if warning:
        result["warning"] = warning
    return result
