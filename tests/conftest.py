"""Shared test fixtures for integration and security tests."""
import pytest, pytest_asyncio, asyncio, json, time
from dataclasses import dataclass
import httpx
import redis as redis_lib

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORTAL_URL  = "http://[::1]:8080"
GATEWAY_URL = "http://[::1]:9080"
TXS_URL     = "http://[::1]:8081"
KC_URL      = "http://[::1]:8180"
REDIS_HOST  = "::1"
REDIS_PORT  = 6379
REDIS_PW    = "changeme123"

# Pre-seeded approved application ID
APPROVED_APP_ID = "b2000000-0000-0000-0000-000000000001"
PENDING_APP_ID  = "b2000000-0000-0000-0000-000000000002"
USER_SUB        = "u-test-001"
OWNER_SUB       = "u-test-002"
ADMIN_SUB       = "u-admin-001"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_user_jwt(user_sub: str) -> str:
    """Get mock JWT for a user via token exchange."""
    import requests as req_lib
    r = req_lib.post(
        f"{KC_URL}/realms/hd/protocol/openid-connect/token",
        auth=("citizen-gw-exchanger", "exchanger-secret-xyz"),
        data={"grant_type": "client_credentials"}
    )
    assert r.status_code == 200, f"client_credentials failed: {r.text}"
    svc_token = r.json()["access_token"]

    r2 = req_lib.post(
        f"{KC_URL}/realms/hd/protocol/openid-connect/token",
        auth=("citizen-gw-exchanger", "exchanger-secret-xyz"),
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": svc_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_subject": user_sub,
            "audience": "internal-api-gateway",
            "scope": "openid capi.vendor.read capi.order.read",
        }
    )
    assert r2.status_code == 200, f"token exchange failed: {r2.text}"
    return r2.json()["access_token"]

@dataclass
class PatInfo:
    token: str
    token_id: str
    scopes: list

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def portal_client():
    client = httpx.AsyncClient(base_url=PORTAL_URL, timeout=30)
    yield client
    try:
        await client.aclose()
    except Exception:
        pass

@pytest_asyncio.fixture
async def gateway_client():
    client = httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30)
    yield client
    try:
        await client.aclose()
    except Exception:
        pass

@pytest_asyncio.fixture
async def txs_client():
    client = httpx.AsyncClient(base_url=TXS_URL, timeout=30)
    yield client
    try:
        await client.aclose()
    except Exception:
        pass

@pytest.fixture(scope="session")
def redis_client():
    r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PW, decode_responses=True)
    yield r
    r.close()

@pytest.fixture(scope="session")
def user_jwt():
    return get_user_jwt(USER_SUB)

@pytest.fixture(scope="session")
def owner_jwt():
    return get_user_jwt(OWNER_SUB)

@pytest.fixture(scope="session")
def admin_jwt():
    return get_user_jwt(ADMIN_SUB)

@pytest_asyncio.fixture
async def valid_pat(portal_client, user_jwt) -> PatInfo:
    """Issue a fresh PAT for testing."""
    resp = await portal_client.post(
        "/portal/v1/tokens",
        json={"appId": APPROVED_APP_ID, "tokenName": "test-pat", "validDays": 1},
        headers={"Authorization": f"Bearer {user_jwt}"}
    )
    assert resp.status_code == 201, f"PAT issue failed: {resp.text}"
    data = resp.json()
    info = PatInfo(token=data["token"], token_id=data["tokenId"], scopes=data["scopes"])
    yield info
    # Cleanup: revoke the PAT
    await portal_client.delete(
        f"/portal/v1/tokens/{info.token_id}",
        headers={"Authorization": f"Bearer {user_jwt}"}
    )

@pytest_asyncio.fixture
async def revoked_pat(portal_client, user_jwt) -> PatInfo:
    """Issue a PAT then immediately revoke it."""
    resp = await portal_client.post(
        "/portal/v1/tokens",
        json={"appId": APPROVED_APP_ID, "tokenName": "revoked-test-pat", "validDays": 1},
        headers={"Authorization": f"Bearer {user_jwt}"}
    )
    assert resp.status_code == 201
    data = resp.json()
    info = PatInfo(token=data["token"], token_id=data["tokenId"], scopes=data["scopes"])

    # Revoke immediately
    await portal_client.delete(
        f"/portal/v1/tokens/{info.token_id}",
        headers={"Authorization": f"Bearer {user_jwt}"}
    )
    yield info

def get_gw_headers(pat: PatInfo) -> dict:
    return {"Authorization": f"Bearer {pat.token}"}
