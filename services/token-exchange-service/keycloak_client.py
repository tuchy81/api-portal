"""Keycloak token exchange client with service account token caching."""
import time, logging
import httpx
from config import settings

logger = logging.getLogger("txs.keycloak")

_svc_token_cache: dict = {"token": None, "expires_at": 0}

async def get_exchanger_token() -> str:
    """Get or refresh service account access token (client_credentials)."""
    now = time.time()
    if _svc_token_cache["token"] and now < _svc_token_cache["expires_at"]:
        return _svc_token_cache["token"]

    async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
        resp = await client.post(
            f"{settings.keycloak_url}/realms/{settings.keycloak_realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.exchanger_client_id,
                "client_secret": settings.exchanger_client_secret,
                "scope": "openid",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        _svc_token_cache["token"] = data["access_token"]
        _svc_token_cache["expires_at"] = now + data.get("expires_in", 300) - 50
        return _svc_token_cache["token"]

async def exchange_token(user_sub: str, scopes: list[str]) -> dict:
    """RFC 8693 token exchange: impersonate user_sub with given scopes."""
    svc_token = await get_exchanger_token()
    scope_str = " ".join(scopes)
    user_sub = user_sub.lower()

    async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
        resp = await client.post(
            f"{settings.keycloak_url}/realms/{settings.keycloak_realm}/protocol/openid-connect/token",
            auth=(settings.exchanger_client_id, settings.exchanger_client_secret),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": svc_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_subject": user_sub,
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                # Do NOT pass `audience` here. Real Keycloak (feature=token-exchange)
                # enforces fine-grained client-to-client exchange permissions when
                # audience is set, which our dev realm does not configure. The
                # exchanged JWT still carries aud=internal-api-gateway via the
                # audience protocol mapper on citizen-gw-exchanger.
                "scope": scope_str,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "expires_in": data.get("expires_in", settings.jwt_ttl),
            "jti": _extract_jti(data["access_token"]),
        }

def _extract_jti(token: str) -> str:
    from jose import jwt as jose_jwt
    try:
        return jose_jwt.get_unverified_claims(token).get("jti", "unknown")
    except Exception:
        return "unknown"
