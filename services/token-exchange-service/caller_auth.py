"""Caller authentication for TXS internal endpoints (spec 5.1).

Anyone who can reach this service can mint an impersonation JWT for an
arbitrary user by supplying {tokenId, userSub, scopes} — the gateway's PAT
verification happens upstream and is not re-checked here. So the endpoint
must only accept calls from the gateway.

Two layers:
  1. X-Internal-Key shared secret — the primary control.
  2. allowed_cidr IP allowlist — required by spec 5.1, but weak on its own
     since container IPs are dynamic in Docker/K8s; kept as defense in depth.
"""
import hmac
import ipaddress
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from config import settings

logger = logging.getLogger("txs.caller_auth")


def _denied(detail: str) -> JSONResponse:
    return JSONResponse(
        {"type": "https://cdp-portal.hd.com/errors/CDP-1005",
         "title": "Access denied by policy",
         "status": 403, "code": "CDP-1005", "detail": detail},
        status_code=403,
    )


def _client_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else ""


def _ip_allowed(client_ip: str) -> bool:
    networks = [c.strip() for c in settings.allowed_cidr.split(",") if c.strip()]
    if not networks:
        return True
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for cidr in networks:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            logger.warning(f"Ignoring malformed CIDR in allowed_cidr: {cidr}")
    return False


def check_caller(request: Request) -> JSONResponse | None:
    """Return a 403 response if the caller is not authorized, else None."""
    if settings.internal_api_key:
        provided = request.headers.get("X-Internal-Key", "")
        if not hmac.compare_digest(provided, settings.internal_api_key):
            logger.warning(f"Rejected call from {_client_ip(request)}: bad X-Internal-Key")
            return _denied("Invalid or missing X-Internal-Key")

    client_ip = _client_ip(request)
    if not _ip_allowed(client_ip):
        logger.warning(f"Rejected call from {client_ip}: not in allowed_cidr")
        return _denied(f"Client IP {client_ip} is not in the allowed CIDR list")

    return None


def warn_if_unprotected() -> None:
    """Called at startup so an unset key is visible in the logs."""
    if not settings.internal_api_key:
        logger.warning(
            "INTERNAL_API_KEY is not set — /internal/token-exchange accepts any caller "
            "that passes the IP allowlist. Set it before deploying."
        )
