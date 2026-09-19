"""TC-X: Token exchange test cases."""
import pytest, hashlib
from conftest import get_gw_headers, TXS_URL, USER_SUB

pytestmark = pytest.mark.asyncio

def _scope_hash(scopes: list) -> str:
    return hashlib.sha256(" ".join(sorted(scopes)).encode()).hexdigest()[:8]

def _decode_jwt_unverified(token: str) -> dict:
    from jose import jwt as jose_jwt
    return jose_jwt.get_unverified_claims(token)

# TC-X-01: JWT cache hit → 200
async def test_tc_x_01_cache_hit(gateway_client, redis_client, valid_pat, user_jwt):
    # First call — will trigger exchange and populate cache
    r1 = await gateway_client.get("/capi/v1/vendors", headers=get_gw_headers(valid_pat))
    assert r1.status_code == 200, f"First call failed: {r1.status_code}"

    sh = _scope_hash(valid_pat.scopes)
    cache_key = f"cdp:jwt:{valid_pat.token_id}:{sh}"
    jwt_in_cache = redis_client.get(cache_key)
    assert jwt_in_cache is not None, "JWT should be in cache after first call"

    # Second call — should use cache
    r2 = await gateway_client.get("/capi/v1/vendors", headers=get_gw_headers(valid_pat))
    assert r2.status_code == 200, f"Cache hit call failed: {r2.status_code}"
    print(f"✓ TC-X-01: JWT cache populated and hit on second call")

# TC-X-02: Circuit breaker OPEN + no cache → 503 CDP-2001
async def test_tc_x_02_circuit_open_no_cache(gateway_client, redis_client, valid_pat):
    sh = _scope_hash(valid_pat.scopes)
    cache_key = f"cdp:jwt:{valid_pat.token_id}:{sh}"

    # Clear JWT cache
    redis_client.delete(cache_key)

    # Force circuit breaker OPEN
    redis_client.setex("cdp:cb:txs", 30, "OPEN")

    try:
        resp = await gateway_client.get("/capi/v1/vendors", headers=get_gw_headers(valid_pat))
        assert resp.status_code == 503, f"Expected 503, got {resp.status_code}: {resp.text}"
        assert resp.json().get("code") == "CDP-2001"
        print(f"✓ TC-X-02: 503 CDP-2001 when circuit OPEN and no cache")
    finally:
        redis_client.delete("cdp:cb:txs")

# TC-X-03: Inspect JWT claims from TXS
async def test_tc_x_03_jwt_claims_validation(txs_client, valid_pat):
    resp = await txs_client.post(
        "/internal/token-exchange",
        json={
            "tokenId": valid_pat.token_id,
            "userSub": USER_SUB,
            "scopes": ["capi.vendor.read"],
        }
    )
    assert resp.status_code == 200, f"TXS failed: {resp.status_code} {resp.text}"
    data = resp.json()
    jwt_str = data["accessToken"]
    assert jwt_str, "accessToken must not be empty"

    claims = _decode_jwt_unverified(jwt_str)
    print(f"TC-X-03 claims: {claims}")

    assert "internal-api-gateway" in claims.get("aud", []), \
        f"aud should contain 'internal-api-gateway', got {claims.get('aud')}"
    assert claims.get("sub") == USER_SUB, \
        f"sub should be {USER_SUB}, got {claims.get('sub')}"
    assert "cdp_channel" in claims, "cdp_channel claim should be present"
    assert claims["cdp_channel"] == "citizen", "cdp_channel should be 'citizen'"

    scope = claims.get("scope", "")
    assert "capi.vendor.read" in scope, f"scope should contain capi.vendor.read, got {scope}"
    print(f"✓ TC-X-03: JWT claims validated — aud, sub, scope, cdp_channel all correct")
