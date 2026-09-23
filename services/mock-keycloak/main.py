"""Mock Keycloak OIDC server — generates real RSA-signed JWTs for testing."""
import json, os, time, uuid, base64, hashlib
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from jose import jwt as jose_jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# ---------------------------------------------------------------------------
# Startup: ensure certs exist
# ---------------------------------------------------------------------------
CERT_DIR = Path(__file__).parent / "certs"
CERT_DIR.mkdir(exist_ok=True)

def _ensure_certs():
    import subprocess, sys
    if not (CERT_DIR / "private.pem").exists():
        subprocess.run([sys.executable, str(CERT_DIR / "gen_keys.py")], check=True)

_ensure_certs()

PRIVATE_KEY_PEM = (CERT_DIR / "private.pem").read_bytes()
JWKS = json.loads((CERT_DIR / "jwks.json").read_text())

ISSUER = os.getenv("KC_ISSUER", "http://mock-keycloak:8180/realms/hd")
KC_CLIENT_ID = os.getenv("KC_CLIENT_ID", "citizen-gw-exchanger")
KC_CLIENT_SECRET = os.getenv("KC_CLIENT_SECRET", "exchanger-secret-xyz")
TOKEN_TTL = int(os.getenv("JWT_TTL", "300"))

# ---------------------------------------------------------------------------
# Mock registries
# ---------------------------------------------------------------------------
MOCK_USERS = {
    "u-test-001": {
        "username": "hong.gildong", "enabled": True, "roles": ["citizen-developer", "mdm-reader"],
        # HR/org test claims for the pat-token-exchange claim -> header mapping.
        # ASCII-only: raw HTTP header values are Latin-1/ASCII by convention,
        # so non-ASCII (e.g. Korean) text here gets mangled by any consumer
        # that decodes headers as Latin-1 (Python/Starlette does) — real
        # "_cd" claims are codes anyway, not free-text names.
        "user_id": "EMP10001", "company": "HDHI", "org_cd": "ORG-IT",
        "asgn_cd": "ASG-DEV", "dept_cd": "DEPT-IT01", "job_tit_cd": "JOB-STAFF",
        "offi_res_cd": "RES-SEOUL", "user_origin": "INTERNAL",
    },
    "u-test-002": {
        "username": "api.owner", "enabled": True, "roles": ["api-owner", "citizen-developer"],
        "user_id": "EMP10002", "company": "HDHI", "org_cd": "ORG-MDM",
        "asgn_cd": "ASG-OWNER", "dept_cd": "DEPT-MDM01", "job_tit_cd": "JOB-LEAD",
        "offi_res_cd": "RES-SEOUL", "user_origin": "INTERNAL",
    },
    "u-admin-001": {
        "username": "platform.admin", "enabled": True, "roles": ["platform-admin", "citizen-developer"],
        "user_id": "EMP90001", "company": "HDHI", "org_cd": "ORG-PLATFORM",
        "asgn_cd": "ASG-ADMIN", "dept_cd": "DEPT-PLAT01", "job_tit_cd": "JOB-MANAGER",
        "offi_res_cd": "RES-SEOUL", "user_origin": "INTERNAL",
    },
    "a453587": {
        "username": "lee.changyob", "enabled": True, "roles": ["citizen-developer"],
        "user_id": "a453587", "company": "HDHI", "org_cd": "ORG-IT",
        "asgn_cd": "ASG-DEV", "dept_cd": "DEPT-IT01", "job_tit_cd": "JOB-STAFF",
        "offi_res_cd": "RES-SEOUL", "user_origin": "INTERNAL",
    },
}

# Claim keys copied from MOCK_USERS onto the token-exchange response's JWT
# (kept as one list so it stays in sync with pat-token-exchange.lua's
# CLAIM_HEADER_MAP without duplicating the mapping itself here).
HR_ORG_CLAIM_KEYS = [
    "user_id", "company", "org_cd", "asgn_cd", "dept_cd", "job_tit_cd", "offi_res_cd", "user_origin",
]

CLIENTS = {
    KC_CLIENT_ID: {"secret": KC_CLIENT_SECRET, "service_account": True}
}

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def _sign_jwt(claims: dict) -> str:
    return jose_jwt.encode(claims, PRIVATE_KEY_PEM, algorithm="RS256",
                           headers={"kid": "mock-key-1"})

def _base_claims(sub: str, roles: list, scopes: list, extra: dict = None) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": sub,
        "aud": ["internal-api-gateway"],
        "azp": KC_CLIENT_ID,
        "exp": now + TOKEN_TTL,
        "iat": now,
        "jti": uuid.uuid4().hex,
        "scope": " ".join(scopes),
        "realm_access": {"roles": roles},
        "cdp_channel": "citizen",
    }
    if extra:
        claims.update(extra)
    return claims

def _verify_basic_auth(authorization: Optional[str], expected_client: str, expected_secret: str) -> bool:
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:]).decode()
        client_id, secret = decoded.split(":", 1)
        return client_id == expected_client and secret == expected_secret
    except Exception:
        return False

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Mock Keycloak")

@app.get("/realms/hd/.well-known/openid-configuration")
def oidc_discovery():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
        "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
        "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
        "grant_types_supported": [
            "authorization_code",
            "client_credentials",
            "urn:ietf:params:oauth:grant-type:token-exchange"
        ],
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }

@app.get("/realms/hd/protocol/openid-connect/certs")
def jwks():
    return JWKS

@app.post("/realms/hd/protocol/openid-connect/token")
async def token_endpoint(
    request: Request,
    authorization: Optional[str] = Header(None),
    grant_type: str = Form(...),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    scope: Optional[str] = Form(None),
    subject_token: Optional[str] = Form(None),
    subject_token_type: Optional[str] = Form(None),
    requested_subject: Optional[str] = Form(None),
    requested_token_type: Optional[str] = Form(None),
    audience: Optional[str] = Form(None),
):
    # Resolve client credentials from Basic auth or form params
    if authorization and authorization.startswith("Basic "):
        try:
            decoded = base64.b64decode(authorization[6:]).decode()
            client_id, client_secret = decoded.split(":", 1)
        except Exception:
            raise HTTPException(400, "Invalid Basic auth")

    if client_id not in CLIENTS or CLIENTS[client_id]["secret"] != client_secret:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    requested_scopes = scope.split() if scope else []

    # --- client_credentials ---
    if grant_type == "client_credentials":
        claims = _base_claims(
            sub=f"service-account-{client_id}",
            roles=["service-account"],
            scopes=requested_scopes or ["openid"],
        )
        token = _sign_jwt(claims)
        return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL}

    # --- token-exchange (RFC 8693 V1 with requested_subject) ---
    if grant_type == "urn:ietf:params:oauth:grant-type:token-exchange":
        if not requested_subject:
            return JSONResponse({"error": "invalid_request", "error_description": "requested_subject required"}, status_code=400)

        user = MOCK_USERS.get(requested_subject)
        if not user:
            return JSONResponse({"error": "invalid_request", "error_description": f"User {requested_subject} not found"}, status_code=400)

        if not user["enabled"]:
            return JSONResponse({"error": "invalid_request", "error_description": "User disabled"}, status_code=400)

        # Downscope: only return what was requested, intersected with user's available scopes
        allowed_user_scopes = set(f"capi.{role.split('-')[1]}.read" for role in user["roles"] if "-" in role)
        allowed_user_scopes.update(["openid", "profile"])
        final_scopes = [s for s in requested_scopes if s in allowed_user_scopes or s.startswith("capi.")] if requested_scopes else list(allowed_user_scopes)

        extra = {"cdp_channel": "citizen", "preferred_username": user["username"]}
        for key in HR_ORG_CLAIM_KEYS:
            if key in user:
                extra[key] = user[key]

        claims = _base_claims(
            sub=requested_subject,
            roles=user["roles"],
            scopes=final_scopes,
            extra=extra,
        )
        token = _sign_jwt(claims)
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL,
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

@app.get("/admin/realms/hd/users/{user_sub}")
def get_user(user_sub: str, authorization: Optional[str] = Header(None)):
    user = MOCK_USERS.get(user_sub)
    if not user:
        raise HTTPException(404, f"User {user_sub} not found")
    return {
        "id": user_sub,
        "username": user["username"],
        "enabled": user["enabled"],
        "realmRoles": user["roles"],
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "mock-keycloak"}
