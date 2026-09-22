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
    ownerDept: Optional[str] = None
    upstreamUrl: Optional[str] = None
    publicPath: Optional[str] = None
    requiredRoles: Optional[list[str]] = None
    scopes: Optional[list[dict]] = None  # full replace when provided: [{scopeName, httpMethod, pathPattern, description}]
    openapiSpec: Optional[dict] = None


def _assert_can_modify(row: asyncpg.Record, user: UserClaims, action: str, api_id: str):
    """None if the caller may modify/delete this catalog row, else a 403
    cdp_error response. Only the API's own owner or a platform-admin may
    act on it — merely holding the api-owner role isn't enough, otherwise
    any API owner could edit or delete someone else's API (mirrors the
    PAT-ownership check in routers/tokens.py's revoke_pat)."""
    if "platform-admin" in user.roles:
        return None
    if "api-owner" not in user.roles:
        return cdp_error("CDP-4003", f"Only API owners or admins can {action} APIs", f"/catalog/apis/{api_id}")
    if row["owner_sub"] != user.sub:
        return cdp_error("CDP-4003", f"Cannot {action} another owner's API", f"/catalog/apis/{api_id}")
    return None

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
    row = await db.fetchrow("SELECT * FROM cdp.api_catalog WHERE api_id=$1", uuid.UUID(api_id))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "API not found"})

    denied = _assert_can_modify(row, user, "modify", api_id)
    if denied:
        return denied

    if all(v is None for v in (
        req.status, req.name, req.description, req.ownerDept, req.upstreamUrl,
        req.publicPath, req.requiredRoles, req.scopes, req.openapiSpec,
    )):
        raise HTTPException(400, detail="No fields to update")

    if req.scopes is not None:
        async with db.transaction():
            await db.execute("DELETE FROM cdp.api_scope WHERE api_id=$1", uuid.UUID(api_id))
            for scope in req.scopes:
                await db.execute(
                    """INSERT INTO cdp.api_scope (api_id, scope_name, http_method, path_pattern, description)
                       VALUES ($1,$2,$3,$4,$5)""",
                    uuid.UUID(api_id), scope["scopeName"], scope["httpMethod"],
                    scope["pathPattern"], scope.get("description")
                )

    # Keep the gateway route in sync with anything that changes what APISIX
    # needs to know (path, upstream, scopes) or whether the route should
    # exist at all (status) — and do it BEFORE committing `status` to the DB,
    # mirroring create_api: the DB must never claim PUBLISHED for a route
    # that wasn't actually (re)registered. Getting this order backwards is
    # exactly how a PATCH can report success while the gateway 404s the
    # public path (a real bug this fixes — see 2/2 catalog test).
    requested_status = req.status if req.status is not None else row["status"]
    new_public_path = req.publicPath if req.publicPath is not None else row["public_path"]
    new_upstream_url = req.upstreamUrl if req.upstreamUrl is not None else row["upstream_url"]
    route_relevant_changed = any([
        req.status is not None and req.status != row["status"],
        req.publicPath is not None and req.publicPath != row["public_path"],
        req.upstreamUrl is not None and req.upstreamUrl != row["upstream_url"],
        req.scopes is not None,
    ])

    final_status = requested_status
    warning = None
    if route_relevant_changed:
        try:
            if requested_status == "PUBLISHED":
                if row["status"] == "PUBLISHED" and new_public_path != row["public_path"]:
                    # public_path drives the APISIX route id (route_id_for) —
                    # a changed path means a different route, so the old one
                    # would otherwise be orphaned in APISIX.
                    await apisix_client.delete_route(row["api_code"], row["public_path"])
                scopes = await db.fetch("SELECT * FROM cdp.api_scope WHERE api_id=$1", uuid.UUID(api_id))
                api_row = {"api_code": row["api_code"], "upstream_url": new_upstream_url, "public_path": new_public_path}
                scope_rows = [{"scope_name": s["scope_name"], "http_method": s["http_method"]} for s in scopes]
                await apisix_client.upsert_route(api_row, scope_rows)
                if not await apisix_client.smoke_test(new_public_path):
                    raise RuntimeError("smoke test did not get the expected 401 from pat-auth")
            elif row["status"] == "PUBLISHED":
                await apisix_client.delete_route(row["api_code"], row["public_path"])
        except Exception as e:
            logger.warning(f"gateway route sync failed for {row['api_code']} ({requested_status}): {e}")
            warning = "API Gateway(APISIX)에 등록되지 않았습니다. 추후 다시 등록해 주세요."
            if requested_status == "PUBLISHED":
                # The route isn't actually live — don't let the DB claim
                # otherwise. Falls back to DRAFT rather than the old status,
                # since upstream_url/public_path/scopes may have already
                # changed underneath the previously-live route.
                final_status = "DRAFT"

    updates = []
    values = []
    idx = 1
    scalar_fields = {
        "status": final_status if (req.status is not None or final_status != row["status"]) else None,
        "name": req.name,
        "description": req.description,
        "owner_dept": req.ownerDept,
        "upstream_url": req.upstreamUrl,
        "public_path": req.publicPath,
        "required_roles": req.requiredRoles,
    }
    for column, value in scalar_fields.items():
        if value is not None:
            updates.append(f"{column}=${idx}"); values.append(value); idx += 1
    if req.openapiSpec is not None:
        updates.append(f"openapi_spec=${idx}::jsonb"); values.append(json.dumps(req.openapiSpec)); idx += 1

    if updates:
        updates.append("updated_at=now()")
        values.append(uuid.UUID(api_id))
        await db.execute(
            f"UPDATE cdp.api_catalog SET {', '.join(updates)} WHERE api_id=${idx}",
            *values
        )

    result = {"apiId": api_id, "updated": True, "status": final_status}
    if warning:
        result["warning"] = warning
    return result


@router.delete("/{api_id}", status_code=204)
async def delete_api(
    api_id: str,
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    row = await db.fetchrow("SELECT * FROM cdp.api_catalog WHERE api_id=$1", uuid.UUID(api_id))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "API not found"})

    denied = _assert_can_modify(row, user, "delete", api_id)
    if denied:
        return denied

    # cdp.application.api_id -> cdp.api_catalog has no ON DELETE CASCADE
    # (usage history is meant to survive an API's removal), so a hard
    # DELETE would fail the FK constraint once any application references
    # this API. Surface that as a clear conflict instead of a raw DB error,
    # and point the caller at the existing RETIRED-status path (PATCH),
    # which is how this catalog already models "no longer available"
    # without breaking that history.
    in_use = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM cdp.application WHERE api_id=$1)", uuid.UUID(api_id)
    )
    if in_use:
        return cdp_error(
            "CDP-4009",
            "API has existing applications and cannot be permanently deleted; "
            "set status to RETIRED instead via PATCH /catalog/apis/{api_id}",
            f"/catalog/apis/{api_id}",
        )

    if row["status"] == "PUBLISHED":
        try:
            await apisix_client.delete_route(row["api_code"], row["public_path"])
        except Exception as e:
            logger.warning(f"gateway route delete failed for {row['api_code']}: {e}")

    await db.execute("DELETE FROM cdp.api_catalog WHERE api_id=$1", uuid.UUID(api_id))
    logger.info(f"API {row['api_code']} deleted by {user.sub}")
