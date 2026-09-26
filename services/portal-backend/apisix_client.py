"""APISIX Admin API client — catalog registration -> route auto-provisioning
(spec section 8). Builds a route from an api_catalog row + its api_scope
rows and PUTs it to APISIX; the plugin chain mirrors what
services/apisix-gateway/conf/config.yaml enables (pat-auth, pat-quota,
pat-token-exchange, proxy-rewrite, pat-audit).
"""
import logging
import re
from urllib.parse import urlparse

import httpx

from config import settings

logger = logging.getLogger("portal.apisix_client")


def route_id_for(api_code: str, public_path: str) -> str:
    """capi-{api_code}-{version}, e.g. capi-mdm-vendor-v1 (spec section 8)."""
    version_match = re.search(r"/v(\d+)(?:/|$)", public_path)
    version = f"v{version_match.group(1)}" if version_match else "v1"
    code = re.sub(r"[^a-z0-9]+", "-", api_code.lower()).strip("-")
    return f"capi-{code}-{version}"


_PARAM_SEGMENT = re.compile(r"^(?:\{[^/{}]+\}|:[A-Za-z_][A-Za-z0-9_]*)$")


def _path_pattern_to_regex(pattern: str) -> str:
    """Convert a registered scope `path_pattern` into an anchored PCRE that
    pat-auth matches the real request URI against. Supports:
      - a plain literal path                 -> exact match only
      - `{param}` / `:param` segments        -> match any single path segment
      - a trailing `/**`                     -> also match any sub-path
    e.g. "/capi/v1/vendors/{id}" -> "^/capi/v1/vendors/[^/]+$", which matches
    "/capi/v1/vendors/123" but not "/capi/v1/vendors" or ".../123/orders"."""
    pattern = pattern.rstrip("/")
    is_wildcard = pattern.endswith("/**")
    if is_wildcard:
        pattern = pattern[: -len("/**")]
    segments = [
        "[^/]+" if _PARAM_SEGMENT.match(seg) else re.escape(seg)
        for seg in pattern.split("/")
    ]
    body = "/".join(segments)
    return f"^{body}(?:/.*)?$" if is_wildcard else f"^{body}$"


def _pattern_specificity(pattern: str) -> tuple:
    """Sort key so pat-auth tries the most specific patterns first: exact
    literals, then `{param}` segments, then a trailing `/**` wildcard last —
    otherwise a broad `/vendors/**` scope registered for the same method
    could shadow a narrower `/vendors/{id}` one."""
    is_wildcard = pattern.rstrip("/").endswith("/**")
    param_count = len(re.findall(r"\{[^/{}]+\}|:[A-Za-z_][A-Za-z0-9_]*", pattern))
    return (1 if is_wildcard else 0, param_count, -len(pattern))


def _rewrite_pattern(public_path: str) -> str:
    """Only the `/capi/vN` version prefix gets swapped for the upstream's
    base path — everything after it (resource name, sub-paths, ids) is
    forwarded unchanged. `upstream_url` is a per-API *base* URL (e.g.
    ".../internal/api/v1", no resource name), so stripping the whole
    public_path here — instead of just its version prefix — would silently
    drop the resource segment from the upstream request."""
    version_match = re.match(r"^/capi/v\d+", public_path)
    prefix = version_match.group(0) if version_match else public_path
    return f"^{re.escape(prefix)}(.*)$"


def _build_route(api: dict, scopes: list[dict]) -> dict:
    public_path = api["public_path"].rstrip("/")
    parsed = urlparse(api["upstream_url"])
    upstream_path = parsed.path.rstrip("/")

    # method -> list of {pattern, scope}, most-specific pattern first (spec
    # section 6.2 note: two scopes can share a method but target different
    # sub-resources via path_pattern, e.g. GET /vendors vs GET /vendors/{id}).
    scopes_by_method: dict[str, list[dict]] = {}
    methods = set()
    for s in scopes:
        method = s["http_method"].upper()
        methods.add(method)
        scopes_by_method.setdefault(method, []).append(s)
    if not methods:
        # No scopes defined yet — still register the route so it 404s
        # cleanly instead of leaving a dangling public_path, but don't
        # grant any method through pat-auth.
        methods = {"GET"}
    # A browser preflight (e.g. Swagger UI's "Try it out") sends an OPTIONS
    # request before the real call whenever it carries an Authorization
    # header. Without OPTIONS in the route's own method list, APISIX's route
    # matching rejects it with 404 before the cors plugin (below) ever gets a
    # chance to answer it — so this has to be added unconditionally.
    methods.add("OPTIONS")

    required_scope_map = {}
    for method, method_scopes in scopes_by_method.items():
        method_scopes.sort(key=lambda s: _pattern_specificity(s["path_pattern"]))
        required_scope_map[method] = [
            {"pattern": _path_pattern_to_regex(s["path_pattern"]), "scope": s["scope_name"]}
            for s in method_scopes
        ]

    common_redis = {
        "redis_host": settings.redis_host,
        "redis_port": settings.redis_port,
        "redis_password": settings.redis_password,
    }

    return {
        "uris": [public_path, public_path + "/*"],
        "methods": sorted(methods),
        "status": 1,
        "priority": 100,
        "upstream": {
            "type": "roundrobin",
            "scheme": parsed.scheme or "http",
            "nodes": {parsed.netloc: 1},
            "timeout": {"connect": 3, "send": 30, "read": 30},
            "retries": 1,
        },
        "plugins": {
            # Dev-permissive defaults (allow_origins/methods/headers = "*").
            # Runs at priority 4000, ahead of pat-auth (3010), so it answers
            # a browser's CORS preflight (OPTIONS) before auth ever sees it —
            # needed for the portal's embedded Swagger UI "Try it out".
            "cors": {},
            "pat-auth": {
                **common_redis,
                "server_key": settings.server_key,
                "portal_backend_url": settings.portal_backend_internal_url,
                "internal_api_key": settings.internal_api_key,
                "neg_cache_ttl": 30,
                "required_scope_map": required_scope_map,
            },
            "pat-quota": common_redis,
            "pat-token-exchange": {
                **common_redis,
                "txs_url": settings.txs_url,
                "internal_api_key": settings.internal_api_key,
            },
            "proxy-rewrite": {
                "regex_uri": [_rewrite_pattern(public_path), f"{upstream_path}$1"],
            },
            "pat-audit": {
                **common_redis,
                "portal_backend_url": settings.portal_backend_internal_url,
                "internal_api_key": settings.internal_api_key,
                "audit_batch_size": 100,
                "audit_flush_interval_s": 5,
            },
        },
    }


def _admin_headers() -> dict:
    return {"X-API-KEY": settings.apisix_admin_key}


async def upsert_route(api: dict, scopes: list[dict]) -> str:
    """PUT the route for this catalog entry. Idempotent — same id in, same
    route out, safe to call again on every portal-backend startup or on a
    catalog PATCH. Raises httpx.HTTPStatusError on failure."""
    route_id = route_id_for(api["api_code"], api["public_path"])
    route = _build_route(api, scopes)
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.put(
            f"{settings.apisix_admin_url}/apisix/admin/routes/{route_id}",
            json=route,
            headers=_admin_headers(),
        )
        resp.raise_for_status()
    logger.info(f"apisix route upserted: {route_id} -> {api['public_path']}")
    return route_id


async def delete_route(api_code: str, public_path: str) -> None:
    """DELETE a route. A 404 from APISIX (already gone) is not an error."""
    route_id = route_id_for(api_code, public_path)
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.delete(
            f"{settings.apisix_admin_url}/apisix/admin/routes/{route_id}",
            headers=_admin_headers(),
        )
        if resp.status_code not in (200, 404):
            resp.raise_for_status()
    logger.info(f"apisix route deleted: {route_id}")


async def smoke_test(public_path: str) -> bool:
    """Spec section 8: "테스트 PAT 로 1회 호출". We don't provision a live
    PAT+scope grant for a route that was just created (nothing has applied
    for it yet), so instead we confirm the route+plugin chain is actually
    wired: an unauthenticated call must be rejected by pat-auth
    (401 CDP-1002), not fall through to APISIX's route-not-found 404.
    """
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"{settings.gateway_url}{public_path}")
        except httpx.HTTPError as e:
            logger.error(f"apisix smoke test request failed for {public_path}: {e}")
            return False
    ok = resp.status_code == 401
    if not ok:
        logger.error(f"apisix smoke test failed for {public_path}: got {resp.status_code}")
    return ok
