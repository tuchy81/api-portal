"""Route-to-scope mapping configuration (mirrors APISIX required_scope_map)."""
import re

# Maps (method, path_pattern) → required_scope
# Pattern format: regex matching the request path
ROUTE_SCOPE_MAP = [
    # MDM Vendor API
    {"methods": ["GET"],        "pattern": r"^/capi/v1/vendors(/.*)?$", "scope": "capi.vendor.read",  "upstream_prefix": "/internal/api/v1"},
    {"methods": ["POST"],       "pattern": r"^/capi/v1/vendors$",       "scope": "capi.vendor.write", "upstream_prefix": "/internal/api/v1"},
    # Finance Order API
    {"methods": ["GET"],        "pattern": r"^/capi/v1/orders(/.*)?$",  "scope": "capi.order.read",   "upstream_prefix": "/internal/api/v1"},
    # HR Employee API
    {"methods": ["GET"],        "pattern": r"^/capi/v1/employees(/.*)?$","scope": "capi.hr.read",     "upstream_prefix": "/internal/api/v1"},
]

def match_route(method: str, path: str) -> dict | None:
    """Find matching route config for method+path. Returns config dict or None."""
    for route in ROUTE_SCOPE_MAP:
        if method.upper() in route["methods"] and re.match(route["pattern"], path):
            return route
    return None

def get_upstream_path(path: str, route: dict) -> str:
    """Convert public CAPI path to internal upstream path."""
    prefix = route["upstream_prefix"]
    # /capi/v1/vendors → /internal/api/v1/vendors
    capi_part = re.sub(r"^/capi/v1", "", path)
    return prefix + capi_part
