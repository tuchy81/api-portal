"""API Catalog endpoints."""
import json, uuid, logging, re
from typing import Optional
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import asyncpg
from database import get_db
from auth import get_current_user, UserClaims
from error_handlers import cdp_error
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

# Endpoint dict shape (both create and patch):
#   {httpMethod, pathPattern, upstreamUrl, requiredScope, description}
# requiredScope is the OAuth scope pat-auth checks against PAT.scopes; it may
# be shared across multiple endpoints of the same API (e.g. capi.vendor.read
# on GET / and GET /{id}).
class ApiCreateRequest(BaseModel):
    apiCode: str
    name: str
    description: Optional[str] = None
    ownerDept: str
    publicPath: str
    requiredRoles: list[str] = []
    endpoints: list[dict] = []
    openapiSpec: Optional[dict] = None

class ApiPatchRequest(BaseModel):
    status: Optional[str] = None
    description: Optional[str] = None
    name: Optional[str] = None
    ownerDept: Optional[str] = None
    publicPath: Optional[str] = None
    requiredRoles: Optional[list[str]] = None
    endpoints: Optional[list[dict]] = None  # full replace when provided
    openapiSpec: Optional[dict] = None


_PARAM_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}|:([A-Za-z_][A-Za-z0-9_]*)")


def _normalize_endpoint_pattern(path_pattern: str) -> str:
    """Canonical form used to compare two endpoint patterns for equivalence.

    Rules (endpoint dedup spec §4):
      1. Must start with '/'.
      2. Path parameters (`{name}` or `:name`) collapse to `{param}` — the
         parameter name is not part of the route identity as far as APISIX
         is concerned (`_path_to_apisix_uri` erases it in the same way).
      3. Trailing '/' (except root) is stripped — GET /vendors and
         GET /vendors/ hit the same route.
      4. Query string / fragment / wildcards are rejected upstream by
         `_assert_valid_endpoints`; this function assumes those have already
         been vetted.

    Raises ValueError on structurally invalid input (empty segments, missing
    leading slash) so the caller can surface it as a validation error."""
    if not path_pattern or not path_pattern.startswith("/"):
        raise ValueError("pathPattern must start with '/'")
    normalized = _PARAM_TOKEN.sub("{param}", path_pattern)
    if "//" in normalized:
        raise ValueError("pathPattern must not contain consecutive '/' segments")
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized


def _endpoint_key(method: str, path_pattern: str) -> tuple[str, str]:
    """The (method, normalized-path) tuple that determines endpoint identity
    for dedup — same tuple = same APISIX route target."""
    return (method.upper(), _normalize_endpoint_pattern(path_pattern))


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
    parameters, no wildcard. Endpoint-level detail belongs on each
    endpoint's `pathPattern` instead."""
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


def _assert_valid_endpoints(public_path: str, endpoints: list[dict], path_for_error: str = "/catalog/apis"):
    """Endpoint validation (spec §3.2, §3.3, §7.2, §7.3). Ensures each
    endpoint's pathPattern is a relative pattern (not re-including Public
    Base), that upstreamUrl is an absolute URL, and that every `{name}` in
    the upstream template is also declared in the pathPattern — otherwise
    proxy-rewrite would silently drop the parameter."""
    base_stripped = (public_path or "").rstrip("/")
    for i, endpoint in enumerate(endpoints):
        loc = f"endpoints[{i}]"
        for req in ("requiredScope", "httpMethod", "pathPattern", "upstreamUrl"):
            if not endpoint.get(req):
                return cdp_error("CDP-4002", f"{loc}.{req} is required", path_for_error)
        pattern = endpoint["pathPattern"]
        if not pattern.startswith("/"):
            return cdp_error(
                "CDP-4002",
                f"{loc}.pathPattern must be a relative pattern starting with '/' (e.g. '/', '/{{id}}')",
                path_for_error,
            )
        if "?" in pattern or "#" in pattern:
            return cdp_error("CDP-4002", f"{loc}.pathPattern must not contain query string or fragment", path_for_error)
        if "*" in pattern:
            return cdp_error("CDP-4002", f"{loc}.pathPattern must not contain wildcard '*'", path_for_error)
        if base_stripped and (pattern == base_stripped or pattern.startswith(base_stripped + "/")):
            return cdp_error(
                "CDP-4002",
                f"{loc}.pathPattern must not repeat the Public Base ('{public_path}'); "
                f"use only the trailing relative segment (e.g. '/', '/{{id}}')",
                path_for_error,
            )
        try:
            _normalize_endpoint_pattern(pattern)
        except ValueError as e:
            return cdp_error("CDP-4002", f"{loc}.pathPattern: {e}", path_for_error)
        parsed = urlparse(endpoint["upstreamUrl"])
        if not parsed.scheme or not parsed.netloc:
            return cdp_error(
                "CDP-4002",
                f"{loc}.upstreamUrl must be an absolute URL with scheme and host (e.g. http://vendor:8080/api/vendors)",
                path_for_error,
            )
        if parsed.fragment:
            return cdp_error("CDP-4002", f"{loc}.upstreamUrl must not contain a URL fragment", path_for_error)
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


def _assert_no_duplicate_endpoints(endpoints: list[dict], path_for_error: str = "/catalog/apis"):
    """Dedup within the request itself (endpoint dedup spec §6). Two
    endpoints whose (method, normalized-path) collide would create the same
    APISIX route id — the second write would silently overwrite the first,
    which is exactly the bug this check exists to catch before INSERT."""
    seen: dict[tuple[str, str], tuple[int, dict]] = {}
    for i, endpoint in enumerate(endpoints):
        try:
            key = _endpoint_key(endpoint["httpMethod"], endpoint["pathPattern"])
        except ValueError:
            # Pattern shape errors are surfaced by _assert_valid_endpoints;
            # here we only want to catch dedup collisions.
            continue
        if key in seen:
            prior_idx, prior = seen[key]
            return cdp_error(
                "CDP-4010",
                f"endpoints[{i}] duplicates endpoints[{prior_idx}]: "
                f"{key[0]} {endpoint['pathPattern']} normalizes to '{key[1]}' "
                f"(conflicts with '{prior['pathPattern']}')",
                path_for_error,
            )
        seen[key] = (i, endpoint)
    return None


def _endpoint_row_dicts(rows) -> list[dict]:
    """Adapt DB rows / DTOs to the internal shape apisix_client expects."""
    out = []
    for e in rows:
        if isinstance(e, dict):
            out.append(
                {
                    "required_scope": e.get("required_scope") or e.get("requiredScope"),
                    "http_method": e.get("http_method") or e.get("httpMethod"),
                    "path_pattern": e.get("path_pattern") or e.get("pathPattern"),
                    "upstream_url": e.get("upstream_url") or e.get("upstreamUrl"),
                }
            )
        else:
            out.append(
                {
                    "required_scope": e["required_scope"],
                    "http_method": e["http_method"],
                    "path_pattern": e["path_pattern"],
                    "upstream_url": e["upstream_url"],
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
    if isinstance(api.get("openapi_spec"), str):
        api["openapi_spec"] = json.loads(api["openapi_spec"])
    endpoints = await db.fetch("SELECT * FROM cdp.api_endpoint WHERE api_id=$1", uuid.UUID(api_id))
    api["endpoints"] = [dict(e) for e in endpoints]
    return api


@router.get("/{api_id}/endpoints/check")
async def check_endpoint(
    api_id: str,
    httpMethod: str = Query(...),
    pathPattern: str = Query(...),
    db: asyncpg.Connection = Depends(get_db),
    user: UserClaims = Depends(get_current_user),
):
    """Endpoint dedup spec §13: optional pre-check that the frontend can call
    on blur/debounce so the user sees an "already registered" hint before
    submitting the whole form. Returns availability plus, on conflict, the
    exact existing row it clashes with. The actual Create/PATCH re-runs the
    same canonical comparison, so this is UX-only — never a security gate."""
    if not await db.fetchval("SELECT 1 FROM cdp.api_catalog WHERE api_id=$1", uuid.UUID(api_id)):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "API not found"})
    try:
        key = _endpoint_key(httpMethod, pathPattern)
    except ValueError as e:
        return cdp_error("CDP-4002", str(e), f"/catalog/apis/{api_id}/endpoints/check")
    rows = await db.fetch(
        "SELECT http_method, path_pattern, required_scope FROM cdp.api_endpoint WHERE api_id=$1 AND http_method=$2",
        uuid.UUID(api_id), key[0],
    )
    for r in rows:
        try:
            if _endpoint_key(r["http_method"], r["path_pattern"]) == key:
                return {
                    "available": False,
                    "normalizedPattern": key[1],
                    "conflict": {
                        "requiredScope": r["required_scope"],
                        "httpMethod": r["http_method"],
                        "pathPattern": r["path_pattern"],
                    },
                }
        except ValueError:
            continue
    return {"available": True, "normalizedPattern": key[1]}


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
    denied = _assert_valid_endpoints(req.publicPath, req.endpoints)
    if denied:
        return denied
    denied = _assert_no_duplicate_endpoints(req.endpoints)
    if denied:
        return denied

    api_id = uuid.uuid4()
    try:
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
            for endpoint in req.endpoints:
                await db.execute(
                    """INSERT INTO cdp.api_endpoint (api_id, required_scope, http_method, path_pattern, upstream_url, description)
                       VALUES ($1,$2,$3,$4,$5,$6)""",
                    api_id, endpoint["requiredScope"], endpoint["httpMethod"],
                    endpoint["pathPattern"], endpoint["upstreamUrl"], endpoint.get("description")
                )
    except asyncpg.UniqueViolationError as e:
        # Concurrent-insert safety net (dedup spec §9-§10): two requests
        # can both pass the pre-check but only one wins the DB UNIQUE. We
        # surface that as CDP-4010, not the raw asyncpg text.
        return cdp_error(
            "CDP-4010",
            f"Endpoint already exists for this API (concurrent registration): {e.detail or e}",
            f"/catalog/apis",
        )

    api_row = {"api_code": req.apiCode, "public_path": req.publicPath}
    endpoints = _endpoint_row_dicts(req.endpoints)

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
    if req.endpoints is not None:
        denied = _assert_valid_endpoints(effective_public_path, req.endpoints, f"/catalog/apis/{api_id}")
        if denied:
            return denied
        denied = _assert_no_duplicate_endpoints(req.endpoints, f"/catalog/apis/{api_id}")
        if denied:
            return denied

    if all(v is None for v in (
        req.status, req.name, req.description, req.ownerDept,
        req.publicPath, req.requiredRoles, req.endpoints, req.openapiSpec,
    )):
        raise HTTPException(400, detail="No fields to update")

    if req.endpoints is not None:
        # Full-replace semantics: validate against the new set alone, not
        # against the old rows we're about to drop (§8 self-conflict rule).
        try:
            async with db.transaction():
                await db.execute("DELETE FROM cdp.api_endpoint WHERE api_id=$1", uuid.UUID(api_id))
                for endpoint in req.endpoints:
                    await db.execute(
                        """INSERT INTO cdp.api_endpoint (api_id, required_scope, http_method, path_pattern, upstream_url, description)
                           VALUES ($1,$2,$3,$4,$5,$6)""",
                        uuid.UUID(api_id), endpoint["requiredScope"], endpoint["httpMethod"],
                        endpoint["pathPattern"], endpoint["upstreamUrl"], endpoint.get("description")
                    )
        except asyncpg.UniqueViolationError as e:
            return cdp_error(
                "CDP-4010",
                f"Endpoint already exists for this API (concurrent registration): {e.detail or e}",
                f"/catalog/apis/{api_id}",
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
        req.endpoints is not None,
    ])

    final_status = requested_status
    warning = None
    if route_relevant_changed:
        try:
            if requested_status == "PUBLISHED":
                if row["status"] == "PUBLISHED" and new_public_path != row["public_path"]:
                    # A changed Public Base means every endpoint route ID
                    # changes; drop the old prefix explicitly so upsert doesn't
                    # have to guess the previous slug.
                    await apisix_client.delete_api_routes(row["api_code"], row["public_path"])
                endpoints_rows = await db.fetch("SELECT * FROM cdp.api_endpoint WHERE api_id=$1", uuid.UUID(api_id))
                api_row = {"api_code": row["api_code"], "public_path": new_public_path}
                endpoints = _endpoint_row_dicts(endpoints_rows)
                await apisix_client.upsert_api_routes(api_row, endpoints)
                if not await apisix_client.smoke_test(api_row, endpoints):
                    raise RuntimeError("smoke test did not get the expected 401 from pat-auth")
            elif row["status"] == "PUBLISHED":
                await apisix_client.delete_api_routes(row["api_code"], row["public_path"])
        except Exception as e:
            logger.warning(f"gateway route sync failed for {row['api_code']} ({requested_status}): {e}")
            warning = "API Gateway(APISIX)에 등록되지 않았습니다. 추후 다시 등록해 주세요."
            if requested_status == "PUBLISHED":
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

    # Any endpoint/publication/routing change may make previously-issued JWTs
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
