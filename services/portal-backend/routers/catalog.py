"""API Catalog endpoints."""
import json, uuid, logging, re
from typing import Optional
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import asyncpg
import httpx
from database import get_db
from auth import get_current_user, UserClaims
from error_handlers import cdp_error
from config import settings
import apisix_client
import redis_client as rc

logger = logging.getLogger("portal.catalog")
router = APIRouter(prefix="/catalog/apis", tags=["catalog"])


async def _tokens_for_api(db: asyncpg.Connection, api_id: uuid.UUID) -> list[str]:
    """Every active PAT whose application belongs to this API. The scope
    change / retire flow uses this to invalidate cached JWTs for tokens whose
    granted_scopes may now decide differently — the PAT itself is untouched,
    only the (up to 240s stale) JWT cache is dropped so the next call runs a
    fresh Token Exchange."""
    rows = await db.fetch(
        """SELECT p.token_id FROM cdp.pat p
           JOIN cdp.application a ON p.app_id = a.app_id
           WHERE a.api_id = $1 AND p.status = 'ACTIVE'""",
        api_id,
    )
    return [r["token_id"] for r in rows]

# Scope dict shape (both create and patch):
#   {scopeName, httpMethod, pathPattern, upstreamUrl, description}
class ApiCreateRequest(BaseModel):
    apiCode: str
    name: str
    description: Optional[str] = None
    ownerDept: str
    publicPath: str
    requiredRoles: list[str] = []
    scopes: list[dict] = []
    openapiSpec: Optional[dict] = None

class ApiPatchRequest(BaseModel):
    status: Optional[str] = None
    description: Optional[str] = None
    name: Optional[str] = None
    ownerDept: Optional[str] = None
    publicPath: Optional[str] = None
    requiredRoles: Optional[list[str]] = None
    scopes: Optional[list[dict]] = None  # full replace when provided
    openapiSpec: Optional[dict] = None


_PARAM_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}|:([A-Za-z_][A-Za-z0-9_]*)")


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


def _assert_valid_public_base(public_path: str, path_for_error: str = "/catalog/apis"):
    """Public Base rules (spec section 3.1 / 7.1): absolute, no path
    parameters, no wildcard. Endpoint-level detail belongs on each scope's
    `pathPattern` instead — catch the common mistake at registration time
    instead of silently publishing a route nothing can ever hit."""
    if not public_path or not public_path.startswith("/"):
        return cdp_error("CDP-4002", "publicPath must start with '/' (e.g. /capi/v1/vendors)", path_for_error)
    if any(c in public_path for c in "{}:*"):
        return cdp_error(
            "CDP-4002",
            "publicPath must be a literal Public Base without {param}/:param/'*' segments; "
            "put path parameters on each endpoint's pathPattern (e.g. pattern '/{id}')",
            path_for_error,
        )
    return None


def _assert_valid_scopes(public_path: str, scopes: list[dict], path_for_error: str = "/catalog/apis"):
    """Endpoint validation (spec sections 3.2, 3.3, 7.2, 7.3). Ensures each
    scope's pathPattern is a relative pattern (not re-including Public Base),
    that upstreamUrl is an absolute URL, and that every `{name}` in the
    upstream template is also declared in the pathPattern — otherwise
    proxy-rewrite would silently drop the parameter."""
    base_stripped = (public_path or "").rstrip("/")
    for i, scope in enumerate(scopes):
        loc = f"scopes[{i}]"
        for req in ("scopeName", "httpMethod", "pathPattern", "upstreamUrl"):
            if not scope.get(req):
                return cdp_error("CDP-4002", f"{loc}.{req} is required", path_for_error)
        pattern = scope["pathPattern"]
        if not pattern.startswith("/"):
            return cdp_error(
                "CDP-4002",
                f"{loc}.pathPattern must be a relative pattern starting with '/' (e.g. '/', '/{{id}}')",
                path_for_error,
            )
        if base_stripped and (pattern == base_stripped or pattern.startswith(base_stripped + "/")):
            return cdp_error(
                "CDP-4002",
                f"{loc}.pathPattern must not repeat the Public Base ('{public_path}'); "
                f"use only the trailing relative segment (e.g. '/', '/{{id}}')",
                path_for_error,
            )
        parsed = urlparse(scope["upstreamUrl"])
        if not parsed.scheme or not parsed.netloc:
            return cdp_error(
                "CDP-4002",
                f"{loc}.upstreamUrl must be an absolute URL with scheme and host (e.g. http://vendor:8080/api/vendors)",
                path_for_error,
            )
        if parsed.fragment:
            return cdp_error(
                "CDP-4002",
                f"{loc}.upstreamUrl must not contain a URL fragment",
                path_for_error,
            )
        pattern_params = {m.group(1) or m.group(2) for m in _PARAM_TOKEN.finditer(pattern)}
        upstream_params = {m.group(1) or m.group(2) for m in _PARAM_TOKEN.finditer(parsed.path)}
        missing = upstream_params - pattern_params
        if missing:
            return cdp_error(
                "CDP-4002",
                f"{loc}.upstreamUrl references path parameter(s) {sorted(missing)} not declared in pathPattern",
                path_for_error,
            )
    return None


def _scope_row_dicts(rows) -> list[dict]:
    """Adapt DB rows / DTOs to the internal shape apisix_client expects."""
    out = []
    for s in rows:
        if isinstance(s, dict):
            out.append(
                {
                    "scope_name": s.get("scope_name") or s.get("scopeName"),
                    "http_method": s.get("http_method") or s.get("httpMethod"),
                    "path_pattern": s.get("path_pattern") or s.get("pathPattern"),
                    "upstream_url": s.get("upstream_url") or s.get("upstreamUrl"),
                }
            )
        else:
            out.append(
                {
                    "scope_name": s["scope_name"],
                    "http_method": s["http_method"],
                    "path_pattern": s["path_pattern"],
                    "upstream_url": s["upstream_url"],
                }
            )
    return out


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

    denied = _assert_valid_public_base(req.publicPath)
    if denied:
        return denied
    denied = _assert_valid_scopes(req.publicPath, req.scopes)
    if denied:
        return denied

    # Pipeline (spec section 8): DB INSERT as DRAFT -> per-endpoint APISIX
    # route + plugin chain -> smoke test -> PUBLISHED. If the gateway check
    # fails, we don't hard-fail the whole request (the catalog entry itself is
    # fine) — we save it as DRAFT and surface a warning so the owner knows to
    # retry (PATCH {"status": "PUBLISHED"} once fixed, or it self-heals on the
    # next portal-backend startup resync).
    api_id = uuid.uuid4()
    async with db.transaction():
        await db.execute(
            """INSERT INTO cdp.api_catalog
               (api_id, api_code, name, description, owner_dept, owner_sub, public_path, required_roles, openapi_spec, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,'DRAFT')""",
            api_id, req.apiCode, req.name, req.description,
            req.ownerDept, user.sub, req.publicPath,
            req.requiredRoles,
            json.dumps(req.openapiSpec) if req.openapiSpec is not None else None
        )
        for scope in req.scopes:
            await db.execute(
                """INSERT INTO cdp.api_scope (api_id, scope_name, http_method, path_pattern, upstream_url, description)
                   VALUES ($1,$2,$3,$4,$5,$6)""",
                api_id, scope["scopeName"], scope["httpMethod"],
                scope["pathPattern"], scope["upstreamUrl"], scope.get("description")
            )

    api_row = {"api_code": req.apiCode, "public_path": req.publicPath}
    endpoints = _scope_row_dicts(req.scopes)

    status = "DRAFT"
    warning = None
    try:
        await apisix_client.upsert_api_routes(api_row, endpoints)
        if not await apisix_client.smoke_test(api_row, endpoints):
            raise RuntimeError("smoke test did not get the expected 401 from pat-auth")
        status = "PUBLISHED"
    except Exception as e:
        logger.warning(f"gateway route registration check failed for {req.apiCode}: {e}")
        warning = "API Gateway(APISIX)에 등록되지 않았습니다. 추후 다시 등록해 주세요."

    await db.execute(
        "UPDATE cdp.api_catalog SET status=$1, updated_at=now() WHERE api_id=$2", status, api_id
    )

    if status == "PUBLISHED":
        logger.info(f"API {req.apiCode} published by {user.sub} ({len(endpoints)} endpoint route(s) auto-provisioned in APISIX)")
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

    effective_public_path = req.publicPath if req.publicPath is not None else row["public_path"]
    if req.publicPath is not None:
        denied = _assert_valid_public_base(req.publicPath, f"/catalog/apis/{api_id}")
        if denied:
            return denied
    if req.scopes is not None:
        denied = _assert_valid_scopes(effective_public_path, req.scopes, f"/catalog/apis/{api_id}")
        if denied:
            return denied

    if all(v is None for v in (
        req.status, req.name, req.description, req.ownerDept,
        req.publicPath, req.requiredRoles, req.scopes, req.openapiSpec,
    )):
        raise HTTPException(400, detail="No fields to update")

    if req.scopes is not None:
        async with db.transaction():
            await db.execute("DELETE FROM cdp.api_scope WHERE api_id=$1", uuid.UUID(api_id))
            for scope in req.scopes:
                await db.execute(
                    """INSERT INTO cdp.api_scope (api_id, scope_name, http_method, path_pattern, upstream_url, description)
                       VALUES ($1,$2,$3,$4,$5,$6)""",
                    uuid.UUID(api_id), scope["scopeName"], scope["httpMethod"],
                    scope["pathPattern"], scope["upstreamUrl"], scope.get("description")
                )

    # Keep the gateway routes in sync with anything that changes what APISIX
    # needs to know (path, upstream, scopes) or whether the routes should
    # exist at all (status) — and do it BEFORE committing `status` to the DB,
    # mirroring create_api: the DB must never claim PUBLISHED for routes that
    # weren't actually (re)registered.
    requested_status = req.status if req.status is not None else row["status"]
    new_public_path = effective_public_path
    route_relevant_changed = any([
        req.status is not None and req.status != row["status"],
        req.publicPath is not None and req.publicPath != row["public_path"],
        req.scopes is not None,
    ])

    final_status = requested_status
    warning = None
    if route_relevant_changed:
        try:
            if requested_status == "PUBLISHED":
                if row["status"] == "PUBLISHED" and new_public_path != row["public_path"]:
                    # A changed Public Base means every endpoint route ID
                    # changes; drop the old prefix explicitly so upsert doesn't
                    # have to guess the previous slug (`upsert_api_routes`
                    # already prunes stale ids under the current prefix, but
                    # the *previous* public_path may hash into different ids).
                    await apisix_client.delete_api_routes(row["api_code"], row["public_path"])
                scopes = await db.fetch("SELECT * FROM cdp.api_scope WHERE api_id=$1", uuid.UUID(api_id))
                api_row = {"api_code": row["api_code"], "public_path": new_public_path}
                endpoints = _scope_row_dicts(scopes)
                await apisix_client.upsert_api_routes(api_row, endpoints)
                if not await apisix_client.smoke_test(api_row, endpoints):
                    raise RuntimeError("smoke test did not get the expected 401 from pat-auth")
            elif row["status"] == "PUBLISHED":
                await apisix_client.delete_api_routes(row["api_code"], row["public_path"])
        except Exception as e:
            logger.warning(f"gateway route sync failed for {row['api_code']} ({requested_status}): {e}")
            warning = "API Gateway(APISIX)에 등록되지 않았습니다. 추후 다시 등록해 주세요."
            if requested_status == "PUBLISHED":
                # The routes aren't actually live — don't let the DB claim
                # otherwise. Falls back to DRAFT rather than the old status,
                # since public_path/scopes may have already changed underneath
                # the previously-live routes.
                final_status = "DRAFT"

    updates = []
    values = []
    idx = 1
    scalar_fields = {
        "status": final_status if (req.status is not None or final_status != row["status"]) else None,
        "name": req.name,
        "description": req.description,
        "owner_dept": req.ownerDept,
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

    # Any scope/publication/routing change may make previously-issued JWTs
    # obsolete (a scope removed from the API is still baked into a 240s-TTL
    # cached JWT until it expires). Flush JWT cache only — PATs stay valid;
    # next call goes back through Token Exchange with the current scope set.
    if route_relevant_changed:
        token_ids = await _tokens_for_api(db, uuid.UUID(api_id))
        if token_ids:
            try:
                dropped = rc.invalidate_jwt_cache(rc.get_redis(), token_ids)
                logger.info(
                    f"invalidated {dropped} JWT cache entries across {len(token_ids)} "
                    f"PATs after catalog change on {row['api_code']}"
                )
            except Exception as e:
                logger.warning(f"JWT cache invalidation failed for {row['api_code']}: {e}")

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
            await apisix_client.delete_api_routes(row["api_code"], row["public_path"])
        except Exception as e:
            logger.warning(f"gateway route delete failed for {row['api_code']}: {e}")

    await db.execute("DELETE FROM cdp.api_catalog WHERE api_id=$1", uuid.UUID(api_id))
    logger.info(f"API {row['api_code']} deleted by {user.sub}")
