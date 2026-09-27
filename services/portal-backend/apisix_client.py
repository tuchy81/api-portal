"""APISIX Admin API client — catalog registration -> route auto-provisioning
(spec section 8). Post the endpoint-upstream refactor: one APISIX route per
`(method, final_public_path)` endpoint, each with its own upstream node and
proxy-rewrite. The plugin chain (cors, pat-auth, pat-quota, pat-token-exchange,
pat-audit, proxy-rewrite) mirrors what services/apisix-gateway/conf/config.yaml
enables.
"""
import hashlib
import logging
import re
from urllib.parse import urlparse

import httpx

from config import settings

logger = logging.getLogger("portal.apisix_client")


_PARAM_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}|:([A-Za-z_][A-Za-z0-9_]*)")
_PARAM_SEGMENT = re.compile(r"^(?:\{[^/{}]+\}|:[A-Za-z_][A-Za-z0-9_]*)$")


def _slug(api_code: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", api_code.lower()).strip("-")


def _version_of(public_path: str) -> str:
    m = re.search(r"/v(\d+)(?:/|$)", public_path)
    return f"v{m.group(1)}" if m else "v1"


def normalize_public_path(public_base: str, pattern: str) -> str:
    """Join Public Base + relative Endpoint Pattern into a canonical URI.
    `pattern == "/"` collapses to the Public Base itself (spec 3.2)."""
    base = (public_base or "").rstrip("/")
    if not pattern or pattern == "/":
        return base
    return base + "/" + pattern.lstrip("/")


def _extract_param_names(path: str) -> list[str]:
    """Ordered list of `{name}`/`:name` parameter names in a path, first-to-last."""
    return [m.group(1) or m.group(2) for m in _PARAM_TOKEN.finditer(path)]


def _path_to_apisix_uri(final_public_path: str) -> tuple[str | None, list[str] | None]:
    """APISIX's default radixtree matcher doesn't understand `:name` parameters
    (only literal paths and a trailing `*` glob). To match `/capi/v1/vendors/{id}`
    without changing the gateway-wide router mode, register the route with the
    broadest safe wildcard covering the parameterized segment(s) and let a
    per-route `vars` regex do the exact filtering.

    Returns `(uri, None)` for a literal-only path — APISIX can match it directly —
    or `(None, [uris...])` for a parameterized path, where the caller must
    combine those `uris` with a `vars` regex constraint."""
    if not _PARAM_TOKEN.search(final_public_path):
        return final_public_path, None
    prefix = final_public_path[: _PARAM_TOKEN.search(final_public_path).start()].rstrip("/")
    # Two forms so APISIX's tree walk both routes the exact prefix (`/vendors`)
    # and anything deeper (`/vendors/123`, `/vendors/123/orders`). vars filters
    # the final choice down to the endpoint's own shape.
    return None, [prefix, prefix + "/*"]


def _path_to_regex_with_groups(final_public_path: str) -> tuple[str, list[str]]:
    """Turn the endpoint's public path into an anchored PCRE with a capture
    group per path parameter (in declaration order). Used both for pat-auth's
    scope-match regex and for the proxy-rewrite `regex_uri` source pattern."""
    groups: list[str] = []
    parts: list[str] = []
    i = 0
    for m in _PARAM_TOKEN.finditer(final_public_path):
        parts.append(re.escape(final_public_path[i : m.start()]))
        parts.append("([^/]+)")
        groups.append(m.group(1) or m.group(2))
        i = m.end()
    parts.append(re.escape(final_public_path[i:]))
    return f"^{''.join(parts)}$", groups


def _upstream_path_replacement(upstream_path: str, groups: list[str]) -> str:
    """Substitute `{name}`/`:name` tokens in the upstream template path with
    positional refs (`$1`, `$2`, ...) that line up with the source regex
    capture groups produced by `_path_to_regex_with_groups`."""
    index = {name: i + 1 for i, name in enumerate(groups)}

    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name not in index:
            raise ValueError(
                f"upstream path uses '{{{name}}}' which is not declared in the endpoint pattern"
            )
        return f"${index[name]}"

    return _PARAM_TOKEN.sub(repl, upstream_path)


def endpoint_route_id(api_code: str, method: str, final_public_path: str) -> str:
    """Route ID rule (spec section 10):

        capi-{apiCode}-{version}-{method}-{endpointHash}

    where endpointHash = md5("METHOD|final_public_path")[:4]. Same method +
    same public path always yields the same id, so PATCH cycles don't churn
    APISIX routes."""
    h = hashlib.md5(f"{method.upper()}|{final_public_path}".encode()).hexdigest()[:4]
    return f"capi-{_slug(api_code)}-{_version_of(final_public_path)}-{method.lower()}-{h}"


def legacy_api_route_id(api_code: str, public_path: str) -> str:
    """Pre-refactor route id — one route per API. Kept only so
    `delete_legacy_route` can clean it up after migration."""
    return f"capi-{_slug(api_code)}-{_version_of(public_path)}"


def _common_redis() -> dict:
    return {
        "redis_host": settings.redis_host,
        "redis_port": settings.redis_port,
        "redis_password": settings.redis_password,
    }


def build_endpoint_route(api: dict, endpoint: dict) -> dict:
    """Construct one APISIX route for a single (method, pattern, upstream)
    endpoint. Enforces the Section 7 rule that the upstream path's parameter
    names must be a subset of the pattern's — a `{id}` in the upstream with no
    matching `{id}` in the pattern raises here rather than silently 404ing."""
    method = endpoint["http_method"].upper()
    final_public_path = normalize_public_path(api["public_path"], endpoint["path_pattern"])
    upstream_url = endpoint["upstream_url"]
    parsed = urlparse(upstream_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"upstream_url must be an absolute URL with scheme and host: {upstream_url}")
    upstream_path = parsed.path or "/"

    single_uri, wildcard_uris = _path_to_apisix_uri(final_public_path)
    source_regex, groups = _path_to_regex_with_groups(final_public_path)
    upstream_groups = _extract_param_names(upstream_path)
    unknown = [g for g in upstream_groups if g not in groups]
    if unknown:
        raise ValueError(
            f"upstream template references undeclared path parameter(s) {unknown}: {upstream_url}"
        )

    if upstream_groups:
        proxy_rewrite = {"regex_uri": [source_regex, _upstream_path_replacement(upstream_path, groups)]}
    else:
        proxy_rewrite = {"uri": upstream_path}

    required_scope_map = {
        method: [
            {
                # pat-auth compares ctx.var.uri against this anchored PCRE to
                # decide which scope is required — must be the *final* public
                # path, not the relative Endpoint Pattern (spec section 9).
                "pattern": source_regex,
                "scope": endpoint["scope_name"],
            }
        ]
    }

    common_redis = _common_redis()
    # Preflight (OPTIONS) has to be in the route's methods list, otherwise
    # APISIX 404s it before the cors plugin (priority 4000, ahead of pat-auth
    # 3010) can answer — needed for the portal's embedded Swagger UI.
    route: dict = {
        "methods": sorted({method, "OPTIONS"}),
        "status": 1,
        # Literal endpoints (no path params) claim priority 200 so APISIX picks
        # a `/vendors/summary` route over the sibling `/vendors/*` route that
        # covers `/vendors/{id}`. The wildcard route also gets a vars regex to
        # keep the match strictly the shape the endpoint declared.
        "priority": 200 if groups == [] else 100,
        "upstream": {
            "type": "roundrobin",
            "scheme": parsed.scheme,
            "nodes": {parsed.netloc: 1},
            "timeout": {"connect": 3, "send": 30, "read": 30},
            "retries": 1,
        },
        "plugins": {
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
            "proxy-rewrite": proxy_rewrite,
            "pat-audit": {
                **common_redis,
                "portal_backend_url": settings.portal_backend_internal_url,
                "internal_api_key": settings.internal_api_key,
                "audit_batch_size": 100,
                "audit_flush_interval_s": 5,
            },
        },
    }
    if single_uri is not None:
        route["uri"] = single_uri
    else:
        route["uris"] = wildcard_uris
        # `vars` runs after uri matching, so the wildcard-prefixed candidate
        # only wins when the full request URI matches the endpoint's exact
        # shape — this is what stops `/vendors/summary/anything` from being
        # served by the `/vendors/{id}` route.
        route["vars"] = [["uri", "~*", source_regex]]
    return route


def _admin_headers() -> dict:
    return {"X-API-KEY": settings.apisix_admin_key}


async def _put_route(route_id: str, route: dict) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.put(
            f"{settings.apisix_admin_url}/apisix/admin/routes/{route_id}",
            json=route,
            headers=_admin_headers(),
        )
        resp.raise_for_status()


async def _delete_route(route_id: str) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.delete(
            f"{settings.apisix_admin_url}/apisix/admin/routes/{route_id}",
            headers=_admin_headers(),
        )
        if resp.status_code not in (200, 404):
            resp.raise_for_status()


async def _list_route_ids() -> list[str]:
    """All route IDs currently in APISIX. Used to reap orphaned per-endpoint
    routes on catalog PATCH (endpoint removed) and to strip legacy API-level
    routes leftover from before the endpoint-upstream refactor."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(
            f"{settings.apisix_admin_url}/apisix/admin/routes",
            headers=_admin_headers(),
        )
        resp.raise_for_status()
        payload = resp.json()
    # APISIX 3.x returns {"list": [{"value": {"id": "..."}, ...}, ...]}.
    items = payload.get("list") or []
    ids = []
    for item in items:
        value = item.get("value") or {}
        rid = value.get("id") or item.get("key", "").rsplit("/", 1)[-1]
        if rid:
            ids.append(rid)
    return ids


def _owns_route_id(slug: str) -> re.Pattern:
    """Anchored pattern that matches route ids belonging to this API only.
    Using `capi-{slug}-` as a plain prefix would over-match when a longer
    slug shares the shorter one's prefix (e.g. `mdm-vendor` vs.
    `mdm-vendor-legacy`) — the extra `-v\\d+` guard prevents that."""
    return re.compile(rf"^capi-{re.escape(slug)}-v\d+(?:-|$)")


async def upsert_api_routes(api: dict, endpoints: list[dict]) -> list[str]:
    """Idempotently reconcile every APISIX route this catalog entry owns.

    Creates/updates one route per endpoint (route id derived from method +
    final public path so re-runs land on the same object) and deletes any
    route that used to belong to this API but no longer maps to an endpoint —
    both legacy pre-refactor `capi-{code}-{version}` API-level routes and
    per-endpoint routes whose (method, pattern) was removed. Safe to call on
    startup resync and on catalog PATCH."""
    slug = _slug(api["api_code"])
    keep: set[str] = set()
    for ep in endpoints:
        method = ep["http_method"].upper()
        final_public_path = normalize_public_path(api["public_path"], ep["path_pattern"])
        rid = endpoint_route_id(api["api_code"], method, final_public_path)
        keep.add(rid)
        await _put_route(rid, build_endpoint_route(api, ep))
        logger.info(f"apisix route upserted: {rid} -> {method} {final_public_path}")

    try:
        existing = await _list_route_ids()
    except Exception as e:
        # A route-list failure shouldn't block publishing; the next resync will
        # sweep orphans. Log so it isn't silent.
        logger.warning(f"apisix route list failed while pruning {api['api_code']}: {e}")
        existing = []
    owns = _owns_route_id(slug)
    for rid in existing:
        if owns.match(rid) and rid not in keep:
            await _delete_route(rid)
            logger.info(f"apisix route pruned: {rid}")
    return sorted(keep)


async def delete_api_routes(api_code: str, public_path: str) -> None:
    """Remove every APISIX route for this API — both new per-endpoint routes
    and any legacy API-level route from before the endpoint-upstream refactor.
    Used on catalog DELETE and on transitions out of PUBLISHED."""
    slug = _slug(api_code)
    owns = _owns_route_id(slug)
    legacy_rid = legacy_api_route_id(api_code, public_path)
    try:
        existing = await _list_route_ids()
    except Exception as e:
        logger.warning(f"apisix route list failed for {api_code}: {e}")
        # Fall back to at least removing the legacy id we can name directly.
        await _delete_route(legacy_rid)
        return
    for rid in existing:
        if owns.match(rid):
            await _delete_route(rid)
            logger.info(f"apisix route deleted: {rid}")


async def smoke_test(api: dict, endpoints: list[dict]) -> bool:
    """Spec section 8: "테스트 PAT 로 1회 호출". We can't provision a live PAT
    for a route that was just created, so we confirm the plugin chain is
    actually wired by calling one endpoint's final public path without
    credentials and expecting a 401 from pat-auth (not a 404 route-miss).

    Picks the first endpoint whose pattern has no path parameters so the URI
    is directly callable; falls back to the API's Public Base."""
    target_path = None
    for ep in endpoints:
        if "{" not in ep["path_pattern"] and ":" not in ep["path_pattern"]:
            target_path = normalize_public_path(api["public_path"], ep["path_pattern"])
            break
    if target_path is None:
        target_path = api["public_path"].rstrip("/") or "/"
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"{settings.gateway_url}{target_path}")
        except httpx.HTTPError as e:
            logger.error(f"apisix smoke test request failed for {target_path}: {e}")
            return False
    ok = resp.status_code == 401
    if not ok:
        logger.error(f"apisix smoke test failed for {target_path}: got {resp.status_code}")
    return ok
