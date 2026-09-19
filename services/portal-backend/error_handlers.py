"""RFC 9457 Problem+JSON error responses."""
from fastapi.responses import JSONResponse

CDP_ERRORS = {
    "CDP-1001": (401, "Invalid or revoked token"),
    "CDP-1002": (401, "PAT format error"),
    "CDP-1003": (403, "Scope mismatch"),
    "CDP-1004": (429, "Quota exceeded"),
    "CDP-1005": (403, "Access denied by policy"),
    "CDP-1006": (403, "IP CIDR violation"),
    "CDP-2001": (503, "Authorization service unavailable"),
    "CDP-2002": (502, "Upstream error"),
    "CDP-4001": (400, "Request validation failed"),
    "CDP-4003": (403, "Portal authorization denied"),
    "CDP-4009": (409, "Resource state conflict"),
}

def cdp_error(code: str, detail: str, instance: str = "/", trace_id: str = "") -> JSONResponse:
    status, title = CDP_ERRORS.get(code, (500, "Internal error"))
    body = {
        "type": f"https://cdp-portal.hd.com/errors/{code}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        "instance": instance,
    }
    if trace_id:
        body["traceId"] = trace_id
    return JSONResponse(body, status_code=status, media_type="application/problem+json")
