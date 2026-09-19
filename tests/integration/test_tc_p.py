"""TC-P: Portal PAT management test cases."""
import pytest
from conftest import APPROVED_APP_ID, PENDING_APP_ID, get_gw_headers

pytestmark = pytest.mark.asyncio

# TC-P-01: PAT issue with unapproved (PENDING) application → 403 CDP-4003
async def test_tc_p_01_unapproved_app(portal_client, user_jwt):
    resp = await portal_client.post(
        "/portal/v1/tokens",
        json={"appId": PENDING_APP_ID, "tokenName": "test", "validDays": 1},
        headers={"Authorization": f"Bearer {user_jwt}"}
    )
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("code") == "CDP-4003"
    print(f"✓ TC-P-01: 403 CDP-4003 for unapproved application")

# TC-P-02: 4th PAT when limit is 3 → 409 CDP-4009
async def test_tc_p_02_pat_limit(portal_client, redis_client, user_jwt):
    created_ids = []
    try:
        # Issue 3 PATs (max per app)
        for i in range(3):
            resp = await portal_client.post(
                "/portal/v1/tokens",
                json={"appId": APPROVED_APP_ID, "tokenName": f"limit-test-{i}", "validDays": 1},
                headers={"Authorization": f"Bearer {user_jwt}"}
            )
            if resp.status_code == 201:
                created_ids.append(resp.json()["tokenId"])
            elif resp.status_code == 409:
                # Already at limit from previous test runs
                print(f"  Limit reached at PAT {i+1} (from prior test state)")
                break

        # 4th PAT (or next one when limit is hit) → 409
        resp4 = await portal_client.post(
            "/portal/v1/tokens",
            json={"appId": APPROVED_APP_ID, "tokenName": "over-limit-pat", "validDays": 1},
            headers={"Authorization": f"Bearer {user_jwt}"}
        )
        assert resp4.status_code == 409, f"Expected 409, got {resp4.status_code}: {resp4.text}"
        assert resp4.json().get("code") == "CDP-4009"
        print(f"✓ TC-P-02: 409 CDP-4009 when PAT limit exceeded")
    finally:
        # Cleanup all created PATs
        for tid in created_ids:
            await portal_client.delete(
                f"/portal/v1/tokens/{tid}",
                headers={"Authorization": f"Bearer {user_jwt}"}
            )

# TC-P-03: Revoke PAT → gateway returns 401 immediately
async def test_tc_p_03_revoke_immediate_effect(portal_client, gateway_client, user_jwt):
    # Issue a fresh PAT
    resp = await portal_client.post(
        "/portal/v1/tokens",
        json={"appId": APPROVED_APP_ID, "tokenName": "revoke-immediacy-test", "validDays": 1},
        headers={"Authorization": f"Bearer {user_jwt}"}
    )
    assert resp.status_code == 201
    token_data = resp.json()
    pat_token = token_data["token"]
    token_id = token_data["tokenId"]

    # Verify it works
    gw_resp = await gateway_client.get(
        "/capi/v1/vendors",
        headers={"Authorization": f"Bearer {pat_token}"}
    )
    assert gw_resp.status_code == 200, f"PAT should work before revoke: {gw_resp.status_code}"

    # Revoke
    rev_resp = await portal_client.delete(
        f"/portal/v1/tokens/{token_id}",
        headers={"Authorization": f"Bearer {user_jwt}"}
    )
    assert rev_resp.status_code == 204, f"Revoke failed: {rev_resp.status_code}"

    # Immediately try again — should be 401 (Redis cache deleted synchronously)
    gw_resp2 = await gateway_client.get(
        "/capi/v1/vendors",
        headers={"Authorization": f"Bearer {pat_token}"}
    )
    assert gw_resp2.status_code == 401, f"Expected 401 after revoke, got {gw_resp2.status_code}"
    assert gw_resp2.json().get("code") == "CDP-1001"
    print(f"✓ TC-P-03: Revoke immediately reflected — 401 CDP-1001")
