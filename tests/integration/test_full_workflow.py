"""Full E2E workflow test."""
import pytest
from conftest import APPROVED_APP_ID, USER_SUB, get_gw_headers

pytestmark = pytest.mark.asyncio

async def test_full_e2e_workflow(portal_client, gateway_client, redis_client, user_jwt):
    """Complete citizen developer workflow: catalog → apply → PAT → call → usage → revoke."""
    print("\n=== E2E Workflow Test ===")

    # 1. List catalog
    r = await portal_client.get("/portal/v1/catalog/apis", headers={"Authorization": f"Bearer {user_jwt}"})
    assert r.status_code == 200
    apis = r.json()["items"]
    assert len(apis) > 0
    print(f"[1] Catalog: {len(apis)} APIs found")

    # 2. Get API detail
    api_id = str(apis[0]["api_id"])
    r = await portal_client.get(f"/portal/v1/catalog/apis/{api_id}", headers={"Authorization": f"Bearer {user_jwt}"})
    assert r.status_code == 200
    assert "scopes" in r.json()
    print(f"[2] API detail OK: {r.json()['name']}")

    # 3. Issue PAT from pre-approved application
    r = await portal_client.post("/portal/v1/tokens", json={
        "appId": APPROVED_APP_ID,
        "tokenName": "e2e-workflow-test",
        "validDays": 1,
    }, headers={"Authorization": f"Bearer {user_jwt}"})
    assert r.status_code == 201, f"PAT issue failed: {r.text}"
    token_data = r.json()
    pat_token = token_data["token"]
    token_id = token_data["tokenId"]
    print(f"[3] PAT issued: {token_id}, scopes: {token_data['scopes']}")

    try:
        # 4. Call API via gateway with PAT
        r = await gateway_client.get(
            "/capi/v1/vendors",
            headers={"Authorization": f"Bearer {pat_token}"}
        )
        assert r.status_code == 200, f"Gateway call failed: {r.status_code} {r.text}"
        vendors = r.json()["items"]
        assert len(vendors) > 0
        remaining = r.headers.get("X-RateLimit-Remaining", "?")
        print(f"[4] API call OK: {len(vendors)} vendors, remaining={remaining}")

        # 5. Check PAT list
        r = await portal_client.get("/portal/v1/tokens", headers={"Authorization": f"Bearer {user_jwt}"})
        assert r.status_code == 200
        tokens = r.json()["items"]
        my_pat = next((t for t in tokens if t["token_id"] == token_id), None)
        assert my_pat is not None
        assert my_pat["status"] == "ACTIVE"
        print(f"[5] PAT list OK, status=ACTIVE")

        # 6. Check usage statistics
        r = await portal_client.get(
            f"/portal/v1/usage/tokens/{token_id}",
            headers={"Authorization": f"Bearer {user_jwt}"}
        )
        assert r.status_code == 200
        usage = r.json()
        print(f"[6] Usage: today={usage['liveToday']['calls']}/{usage['liveToday']['quota']}")

    finally:
        # 7. Revoke PAT
        r = await portal_client.delete(
            f"/portal/v1/tokens/{token_id}",
            headers={"Authorization": f"Bearer {user_jwt}"}
        )
        assert r.status_code == 204
        print(f"[7] PAT revoked: 204")

    # 8. Verify revocation at gateway
    r = await gateway_client.get("/capi/v1/vendors", headers={"Authorization": f"Bearer {pat_token}"})
    assert r.status_code == 401
    print(f"[8] Post-revoke call: 401 ✓")
    print("=== E2E Workflow PASSED ===")
