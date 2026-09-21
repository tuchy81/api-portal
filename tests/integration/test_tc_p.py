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

# TC-P-04 (R-08): the approver cannot widen or invent scopes. Granted scopes are
# derived server-side from the applicant's request ∩ the catalog definition, so
# a forged grantedScopes in the approval body must have no effect.
async def test_tc_p_04_approver_cannot_set_scopes(portal_client, user_jwt, owner_jwt):
    apis = (await portal_client.get("/portal/v1/catalog/apis",
        headers={"Authorization": f"Bearer {user_jwt}"})).json()["items"]
    vendor_api = next(a for a in apis if a["api_code"] == "MDM-VENDOR")

    created = await portal_client.post("/portal/v1/applications", json={
        "apiId": str(vendor_api["api_id"]),
        "purpose": "scope escalation probe",
        "requestedScopes": ["capi.vendor.read"],
        "validUntil": "2027-03-31",
    }, headers={"Authorization": f"Bearer {user_jwt}"})
    assert created.status_code == 201, created.text
    app_id = created.json()["appId"]

    # Approver tries to grant something never requested and never published.
    approved = await portal_client.patch(f"/portal/v1/applications/{app_id}", json={
        "action": "APPROVE",
        "grantedScopes": ["capi.vendor.write", "capi.hr.read", "totally.made.up"],
    }, headers={"Authorization": f"Bearer {owner_jwt}"})
    assert approved.status_code == 200, approved.text
    granted = approved.json()["grantedScopes"]
    assert granted == ["capi.vendor.read"], f"Approver-supplied scopes leaked through: {granted}"
    print(f"✓ TC-P-04: granted scopes derived server-side, approver input ignored: {granted}")

# TC-P-05 (R-08): scopes not defined in the catalog cannot even be requested.
async def test_tc_p_05_unknown_scope_rejected(portal_client, user_jwt):
    apis = (await portal_client.get("/portal/v1/catalog/apis",
        headers={"Authorization": f"Bearer {user_jwt}"})).json()["items"]
    vendor_api = next(a for a in apis if a["api_code"] == "MDM-VENDOR")

    resp = await portal_client.post("/portal/v1/applications", json={
        "apiId": str(vendor_api["api_id"]),
        "purpose": "unknown scope probe",
        "requestedScopes": ["capi.vendor.read", "capi.secret.admin"],
        "validUntil": "2027-03-31",
    }, headers={"Authorization": f"Bearer {user_jwt}"})
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    assert resp.json().get("code") == "CDP-4001"
    print("✓ TC-P-05: undefined scope rejected at application time")
