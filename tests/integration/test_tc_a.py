"""TC-A: Authentication and authorization test cases."""
import pytest, json
from conftest import get_gw_headers, REDIS_HOST, REDIS_PORT, REDIS_PW

pytestmark = pytest.mark.asyncio

# TC-A-01: Valid PAT, allowed API → 200 + quota headers
async def test_tc_a_01_valid_pat_allowed_api(gateway_client, valid_pat):
    resp = await gateway_client.get(
        "/capi/v1/vendors",
        headers=get_gw_headers(valid_pat)
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert "X-RateLimit-Remaining" in resp.headers, "Missing X-RateLimit-Remaining"
    assert "X-RateLimit-Limit" in resp.headers, "Missing X-RateLimit-Limit"
    data = resp.json()
    assert "items" in data, "Response should have items"
    print(f"✓ TC-A-01: 200 OK, remaining={resp.headers.get('X-RateLimit-Remaining')}")

# TC-A-02: Revoked PAT → 401 CDP-1001 + Redis key absent
async def test_tc_a_02_revoked_pat(gateway_client, redis_client, revoked_pat):
    resp = await gateway_client.get(
        "/capi/v1/vendors",
        headers=get_gw_headers(revoked_pat)
    )
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    body = resp.json()
    assert body.get("code") == "CDP-1001", f"Expected CDP-1001, got {body.get('code')}"
    # Redis key should be absent
    redis_key = f"cdp:pat:{revoked_pat.token_id}"
    assert redis_client.exists(redis_key) == 0, f"Redis key {redis_key} should not exist"
    print(f"✓ TC-A-02: 401 CDP-1001, Redis key absent")

# TC-A-03: Expired PAT (manipulate Redis expires_at to past) → 401 CDP-1001
async def test_tc_a_03_expired_pat(gateway_client, redis_client, valid_pat):
    # Manipulate expires_at in Redis to simulate expiry
    key = f"cdp:pat:{valid_pat.token_id}"
    redis_client.hset(key, "expires_at", "2020-01-01T00:00:00+00:00")
    try:
        resp = await gateway_client.get(
            "/capi/v1/vendors",
            headers=get_gw_headers(valid_pat)
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
        assert resp.json().get("code") == "CDP-1001"
        print(f"✓ TC-A-03: 401 CDP-1001 for expired PAT")
    finally:
        # Restore by deleting key (it'll be re-fetched on next access — but PAT is still being used)
        redis_client.delete(key)

# TC-A-04: Scope mismatch — vendor PAT calling employees endpoint → 403 CDP-1003
async def test_tc_a_04_scope_mismatch(gateway_client, valid_pat):
    # valid_pat has capi.vendor.read, employees requires capi.hr.read
    resp = await gateway_client.get(
        "/capi/v1/employees",
        headers=get_gw_headers(valid_pat)
    )
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"
    body = resp.json()
    assert body.get("code") == "CDP-1003", f"Expected CDP-1003, got {body}"
    print(f"✓ TC-A-04: 403 CDP-1003 for scope mismatch")

# TC-A-05: CIDR restriction — issue PAT with allowedCidr=["192.168.99.0/24"], call from 127.0.0.1 → 403 CDP-1006
async def test_tc_a_05_cidr_violation(gateway_client, portal_client, redis_client, user_jwt):
    from conftest import APPROVED_APP_ID, USER_SUB
    # Issue a PAT with CIDR restriction
    resp = await portal_client.post(
        "/portal/v1/tokens",
        json={
            "appId": APPROVED_APP_ID,
            "tokenName": "cidr-test",
            "validDays": 1,
            "allowedCidr": ["192.168.99.0/24"]  # Not localhost
        },
        headers={"Authorization": f"Bearer {user_jwt}"}
    )
    assert resp.status_code == 201, f"PAT issue failed: {resp.text}"
    token_data = resp.json()
    cidr_pat_token = token_data["token"]
    cidr_token_id = token_data["tokenId"]

    try:
        # Call from "127.0.0.1" context — gateway sees X-Forwarded-For or client.host
        # In our test, the gateway client is calling from localhost (127.0.0.1)
        gw_resp = await gateway_client.get(
            "/capi/v1/vendors",
            headers={"Authorization": f"Bearer {cidr_pat_token}"}
        )
        assert gw_resp.status_code == 403, f"Expected 403 for CIDR violation, got {gw_resp.status_code}"
        assert gw_resp.json().get("code") == "CDP-1006"
        print(f"✓ TC-A-05: 403 CDP-1006 for CIDR violation")
    finally:
        await portal_client.delete(
            f"/portal/v1/tokens/{cidr_token_id}",
            headers={"Authorization": f"Bearer {user_jwt}"}
        )
