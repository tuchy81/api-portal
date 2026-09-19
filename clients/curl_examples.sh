#!/bin/bash
# Zone 1 Client Examples — cURL (spec Appendix A.1)

GATEWAY_URL="http://localhost:9080"
PORTAL_URL="http://localhost:8080"
KC_URL="http://localhost:8180"

# Export your PAT (never hardcode in scripts)
# export CAPI_TOKEN="hdpat_XXXXXXXXXXXX_YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"

echo "=========================================="
echo "1. 정상 호출 — MDM 협력사 목록 조회"
echo "=========================================="
curl -s -X GET "${GATEWAY_URL}/capi/v1/vendors?page=1&size=10" \
  -H "Authorization: Bearer ${CAPI_TOKEN}" \
  -H "Accept: application/json" | python3 -m json.tool

echo ""
echo "=========================================="
echo "2. 응답 헤더 확인 (쿼터 정보)"
echo "=========================================="
curl -i -s -X GET "${GATEWAY_URL}/capi/v1/vendors" \
  -H "Authorization: Bearer ${CAPI_TOKEN}" 2>&1 | grep -E "(HTTP|X-RateLimit|X-Request-Id)"

echo ""
echo "=========================================="
echo "3. Scope 위반 호출 — 403 CDP-1003 예상"
echo "=========================================="
curl -s -X GET "${GATEWAY_URL}/capi/v1/employees" \
  -H "Authorization: Bearer ${CAPI_TOKEN}" \
  -H "Accept: application/json" | python3 -m json.tool

echo ""
echo "=========================================="
echo "4. 잘못된 PAT 형식 — 401 CDP-1002 예상"
echo "=========================================="
curl -s -X GET "${GATEWAY_URL}/capi/v1/vendors" \
  -H "Authorization: Bearer invalid_token_format" | python3 -m json.tool

echo ""
echo "=========================================="
echo "5. 포털 API — 카탈로그 목록 조회 (Bearer 필요)"
echo "=========================================="
echo "먼저 mock-keycloak에서 토큰 발급:"
echo "curl -s -X POST '${KC_URL}/realms/hd/protocol/openid-connect/token' \\"
echo "  -u 'citizen-gw-exchanger:exchanger-secret-xyz' \\"
echo "  -d 'grant_type=client_credentials' | python3 -m json.tool"
