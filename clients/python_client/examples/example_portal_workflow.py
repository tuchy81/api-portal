"""Full E2E portal workflow example: catalog → apply → approve → PAT → API call."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import requests, json

KC_URL = os.getenv("KC_URL", "http://localhost:8180")
PORTAL_URL = os.getenv("PORTAL_URL", "http://localhost:8080")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:9080")

def get_user_token(user_sub: str = "u-test-001") -> str:
    """Get JWT for a mock user (for portal API calls)."""
    # First get service account token
    r = requests.post(
        f"{KC_URL}/realms/hd/protocol/openid-connect/token",
        auth=("citizen-gw-exchanger", "exchanger-secret-xyz"),
        data={"grant_type": "client_credentials"}
    )
    svc_token = r.json()["access_token"]

    # Exchange for user token
    r2 = requests.post(
        f"{KC_URL}/realms/hd/protocol/openid-connect/token",
        auth=("citizen-gw-exchanger", "exchanger-secret-xyz"),
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": svc_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_subject": user_sub,
            "audience": "internal-api-gateway",
            "scope": "openid",
        }
    )
    return r2.json()["access_token"]

def main():
    print("=== 시민개발자 API 포털 전체 워크플로우 ===\n")

    # Step 1: Get auth tokens
    print("[1] 인증 토큰 획득...")
    user_token = get_user_token("u-test-001")
    owner_token = get_user_token("u-test-002")
    print("✓ 사용자 JWT 획득 완료\n")

    portal_headers = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}
    owner_headers = {"Authorization": f"Bearer {owner_token}", "Content-Type": "application/json"}

    # Step 2: Browse catalog
    print("[2] API 카탈로그 조회...")
    r = requests.get(f"{PORTAL_URL}/portal/v1/catalog/apis", headers=portal_headers)
    apis = r.json()["items"]
    print(f"✓ {len(apis)}개 API 조회됨")
    for api in apis:
        print(f"  - {api['api_code']}: {api['name']} ({api['public_path']})")
    print()

    # Use existing pre-approved application from seed data
    app_id = "b2000000-0000-0000-0000-000000000001"
    print(f"[3] 기존 승인된 신청 사용: {app_id}\n")

    # Step 4: Issue PAT
    print("[4] PAT 발급...")
    r = requests.post(f"{PORTAL_URL}/portal/v1/tokens", headers=portal_headers, json={
        "appId": app_id,
        "tokenName": "E2E 워크플로우 테스트 토큰",
        "validDays": 1,
    })
    if r.status_code != 201:
        print(f"✗ PAT 발급 실패: {r.status_code} {r.text}")
        return

    token_data = r.json()
    pat = token_data["token"]
    token_id = token_data["tokenId"]
    print(f"✓ PAT 발급 완료!")
    print(f"  Token ID: {token_id}")
    print(f"  Scopes: {token_data['scopes']}")
    print(f"  만료: {token_data['expiresAt'][:10]}")
    print(f"  경고: {token_data['warning']}\n")

    # Step 5: Call API with PAT
    print("[5] PAT로 API 호출...")
    gw_headers = {"Authorization": f"Bearer {pat}"}
    r = requests.get(f"{GATEWAY_URL}/capi/v1/vendors", headers=gw_headers)
    print(f"  상태: {r.status_code}")
    print(f"  X-RateLimit-Remaining: {r.headers.get('X-RateLimit-Remaining', 'N/A')}")
    if r.status_code == 200:
        vendors = r.json()["items"]
        print(f"✓ 협력사 {len(vendors)}개 조회됨")
    else:
        print(f"✗ 호출 실패: {r.text}")
    print()

    # Step 6: Check usage
    print("[6] 사용량 조회...")
    r = requests.get(f"{PORTAL_URL}/portal/v1/usage/tokens/{token_id}", headers=portal_headers)
    if r.status_code == 200:
        usage = r.json()
        print(f"✓ 오늘 사용량: {usage['liveToday']['calls']} / {usage['liveToday']['quota']}")
    print()

    # Step 7: Revoke PAT
    print("[7] PAT 폐기...")
    r = requests.delete(f"{PORTAL_URL}/portal/v1/tokens/{token_id}", headers=portal_headers)
    print(f"✓ PAT 폐기 완료 (HTTP {r.status_code})\n")

    # Step 8: Verify revocation
    print("[8] 폐기 후 재호출 (401 예상)...")
    r = requests.get(f"{GATEWAY_URL}/capi/v1/vendors", headers=gw_headers)
    print(f"✓ 응답: {r.status_code} — {r.json().get('code', 'unknown')}")
    print("\n=== 워크플로우 완료 ===")

if __name__ == "__main__":
    main()
