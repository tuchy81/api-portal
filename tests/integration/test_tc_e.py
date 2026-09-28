"""TC-E: Endpoint registration / dedup / route conflict test cases.

Covers the two endpoint specs merged into the final unified spec:
  - Endpoint naming (api_scope → api_endpoint, scopeName → requiredScope,
    scopes → endpoints in request/response)
  - Endpoint dedup (canonical pattern normalization, request-internal
    dedup, DB UNIQUE fallback, CDP-4010 conflict)
"""
import uuid
import pytest
from conftest import OWNER_SUB

pytestmark = pytest.mark.asyncio


def _new_api_body(api_code: str, public_path: str, endpoints: list[dict]) -> dict:
    return {
        "apiCode": api_code,
        "name": f"TC-E {api_code}",
        "description": "endpoint dedup test",
        "ownerDept": "TC-E팀",
        "publicPath": public_path,
        "requiredRoles": ["citizen-developer"],
        "endpoints": endpoints,
    }


def _unique_public_path() -> str:
    return f"/capi/v1/tc-e-{uuid.uuid4().hex[:8]}"


def _unique_api_code() -> str:
    return f"TC-E-{uuid.uuid4().hex[:8].upper()}"


async def _cleanup(portal_client, owner_jwt, api_id: str) -> None:
    await portal_client.delete(
        f"/portal/v1/catalog/apis/{api_id}",
        headers={"Authorization": f"Bearer {owner_jwt}"},
    )


# TC-E-01: happy path — endpoints field accepted, response echoes endpoints
async def test_tc_e_01_endpoints_payload(portal_client, owner_jwt):
    body = _new_api_body(
        _unique_api_code(),
        _unique_public_path(),
        [
            {"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/",
             "upstreamUrl": "http://mock-internal-gw:8090/internal/api/v1/vendors", "description": "list"},
            {"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/{id}",
             "upstreamUrl": "http://mock-internal-gw:8090/internal/api/v1/vendors/{id}", "description": "detail"},
        ],
    )
    r = await portal_client.post(
        "/portal/v1/catalog/apis", json=body,
        headers={"Authorization": f"Bearer {owner_jwt}"},
    )
    assert r.status_code == 201, r.text
    api_id = r.json()["apiId"]
    try:
        r2 = await portal_client.get(
            f"/portal/v1/catalog/apis/{api_id}",
            headers={"Authorization": f"Bearer {owner_jwt}"},
        )
        assert r2.status_code == 200
        data = r2.json()
        assert "endpoints" in data
        assert {e["path_pattern"] for e in data["endpoints"]} == {"/", "/{id}"}
        assert all(e["required_scope"] == "capi.tc.read" for e in data["endpoints"])
        print("✓ TC-E-01: endpoints/requiredScope round-trips")
    finally:
        await _cleanup(portal_client, owner_jwt, api_id)


# TC-E-02: duplicate within request — same method + same normalized pattern
async def test_tc_e_02_request_internal_duplicate(portal_client, owner_jwt):
    body = _new_api_body(
        _unique_api_code(),
        _unique_public_path(),
        [
            {"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/vendors",
             "upstreamUrl": "http://mock-internal-gw:8090/api/v"},
            {"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/vendors/",
             "upstreamUrl": "http://mock-internal-gw:8090/api/v"},
        ],
    )
    r = await portal_client.post(
        "/portal/v1/catalog/apis", json=body,
        headers={"Authorization": f"Bearer {owner_jwt}"},
    )
    assert r.status_code == 409, r.text
    assert r.json().get("code") == "CDP-4010"
    print("✓ TC-E-02: trailing-slash dup rejected with CDP-4010")


# TC-E-03: parameter-name-only difference is treated as the same route
async def test_tc_e_03_param_name_conflict(portal_client, owner_jwt):
    body = _new_api_body(
        _unique_api_code(),
        _unique_public_path(),
        [
            {"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/{id}",
             "upstreamUrl": "http://mock-internal-gw:8090/api/v/{id}"},
            {"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/{vendorId}",
             "upstreamUrl": "http://mock-internal-gw:8090/api/v/{vendorId}"},
        ],
    )
    r = await portal_client.post(
        "/portal/v1/catalog/apis", json=body,
        headers={"Authorization": f"Bearer {owner_jwt}"},
    )
    assert r.status_code == 409, r.text
    assert r.json().get("code") == "CDP-4010"
    print("✓ TC-E-03: /{id} vs /{vendorId} rejected (same canonical route)")


# TC-E-04: different methods on same pattern are NOT a conflict
async def test_tc_e_04_different_methods_allowed(portal_client, owner_jwt):
    body = _new_api_body(
        _unique_api_code(),
        _unique_public_path(),
        [
            {"requiredScope": "capi.tc.read",  "httpMethod": "GET",  "pathPattern": "/",
             "upstreamUrl": "http://mock-internal-gw:8090/internal/api/v1/vendors"},
            {"requiredScope": "capi.tc.write", "httpMethod": "POST", "pathPattern": "/",
             "upstreamUrl": "http://mock-internal-gw:8090/internal/api/v1/vendors"},
        ],
    )
    r = await portal_client.post(
        "/portal/v1/catalog/apis", json=body,
        headers={"Authorization": f"Bearer {owner_jwt}"},
    )
    assert r.status_code == 201, r.text
    try:
        pass
    finally:
        await _cleanup(portal_client, owner_jwt, r.json()["apiId"])
    print("✓ TC-E-04: same pattern with different methods coexist")


# TC-E-05: PATCH full-replace with a self-conflicting endpoint set
async def test_tc_e_05_patch_dedup(portal_client, owner_jwt):
    create = await portal_client.post(
        "/portal/v1/catalog/apis",
        json=_new_api_body(
            _unique_api_code(),
            _unique_public_path(),
            [{"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/",
              "upstreamUrl": "http://mock-internal-gw:8090/internal/api/v1/vendors"}],
        ),
        headers={"Authorization": f"Bearer {owner_jwt}"},
    )
    assert create.status_code == 201, create.text
    api_id = create.json()["apiId"]
    try:
        r = await portal_client.patch(
            f"/portal/v1/catalog/apis/{api_id}",
            json={"endpoints": [
                {"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/{id}",
                 "upstreamUrl": "http://mock-internal-gw:8090/api/v/{id}"},
                {"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/{vendorId}",
                 "upstreamUrl": "http://mock-internal-gw:8090/api/v/{vendorId}"},
            ]},
            headers={"Authorization": f"Bearer {owner_jwt}"},
        )
        assert r.status_code == 409, r.text
        assert r.json().get("code") == "CDP-4010"

        # Original endpoint must still be intact (validation ran before DELETE).
        r2 = await portal_client.get(
            f"/portal/v1/catalog/apis/{api_id}",
            headers={"Authorization": f"Bearer {owner_jwt}"},
        )
        patterns = {e["path_pattern"] for e in r2.json()["endpoints"]}
        assert patterns == {"/"}, f"unexpected endpoints after failed PATCH: {patterns}"
        print("✓ TC-E-05: PATCH dedup rejected and left prior endpoints intact")
    finally:
        await _cleanup(portal_client, owner_jwt, api_id)


# TC-E-06: endpoint check API surfaces the concrete conflict
async def test_tc_e_06_endpoint_check(portal_client, owner_jwt):
    create = await portal_client.post(
        "/portal/v1/catalog/apis",
        json=_new_api_body(
            _unique_api_code(),
            _unique_public_path(),
            [{"requiredScope": "capi.tc.read", "httpMethod": "GET", "pathPattern": "/{id}",
              "upstreamUrl": "http://mock-internal-gw:8090/api/v/{id}"}],
        ),
        headers={"Authorization": f"Bearer {owner_jwt}"},
    )
    assert create.status_code == 201, create.text
    api_id = create.json()["apiId"]
    try:
        r = await portal_client.get(
            f"/portal/v1/catalog/apis/{api_id}/endpoints/check",
            params={"httpMethod": "GET", "pathPattern": "/{vendorId}"},
            headers={"Authorization": f"Bearer {owner_jwt}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is False
        assert body["normalizedPattern"] == "/{param}"
        assert body["conflict"]["pathPattern"] == "/{id}"

        r2 = await portal_client.get(
            f"/portal/v1/catalog/apis/{api_id}/endpoints/check",
            params={"httpMethod": "GET", "pathPattern": "/summary"},
            headers={"Authorization": f"Bearer {owner_jwt}"},
        )
        assert r2.json()["available"] is True
        print("✓ TC-E-06: check API detects canonical conflict, allows distinct paths")
    finally:
        await _cleanup(portal_client, owner_jwt, api_id)
