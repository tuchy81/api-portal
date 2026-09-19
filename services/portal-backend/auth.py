"""JWT validation for portal requests."""
import os, time, logging
from typing import Optional
from dataclasses import dataclass
import httpx
from fastapi import Header, HTTPException, Cookie, Query
from jose import jwt as jose_jwt, JWTError
from config import settings

logger = logging.getLogger("portal.auth")

_jwks_cache: dict = {"keys": None, "fetched_at": 0}
ISSUER = f"{settings.kc_url}/realms/{settings.kc_realm}"

@dataclass
class UserClaims:
    sub: str
    username: str
    roles: list[str]
    raw: dict

async def _get_jwks() -> dict:
    now = time.time()
    if _jwks_cache["keys"] is None or now - _jwks_cache["fetched_at"] > 300:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.kc_url}/realms/{settings.kc_realm}/protocol/openid-connect/certs")
            resp.raise_for_status()
            _jwks_cache["keys"] = resp.json()
            _jwks_cache["fetched_at"] = now
    return _jwks_cache["keys"]

async def get_current_user(authorization: Optional[str] = Header(None)) -> UserClaims:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "message": "Missing Bearer token"})

    token = authorization[7:]
    try:
        jwks = await _get_jwks()
        claims = jose_jwt.decode(
            token, jwks,
            algorithms=["RS256"],
            issuer=ISSUER,
            options={"verify_aud": False, "verify_exp": True},
        )
    except JWTError as e:
        raise HTTPException(401, detail={"code": "UNAUTHORIZED", "message": f"Invalid token: {e}"})

    roles = claims.get("realm_access", {}).get("roles", [])
    return UserClaims(
        sub=claims["sub"],
        username=claims.get("preferred_username", claims["sub"]),
        roles=roles,
        raw=claims,
    )

def require_role(*required_roles: str):
    """Returns a dependency that checks if user has at least one required role."""
    async def checker(user: UserClaims = None) -> UserClaims:
        if not any(r in user.roles for r in required_roles):
            raise HTTPException(403, detail={
                "code": "CDP-4003",
                "message": f"Required role(s): {required_roles}",
            })
        return user
    return checker
