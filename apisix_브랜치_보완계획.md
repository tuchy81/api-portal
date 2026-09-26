# APISIX 브랜치 보완 계획서

- 원본 보고서: `apisix_브랜치_종합분석보고서.md`
- 검토 대상: `apisix` 브랜치 실제 코드 + 아키텍처 문서 (`01_시민개발자_API포털_아키텍처정의서.md`, `02_시민개발자_API포털_구현상세스펙.md`)
- 작성일: 2026-09-24
- 목적: 원본 보고서가 지적한 11개 보완항목의 타당성을 코드로 재검증하고, 각 항목에 대한 수용/조정 의견과 구체적 보완 방법·순서를 정리

---

## 0. 요약

원본 보고서가 지적한 11개 항목 중 **9건은 코드 근거로 그대로 확인**되었고, **2건은 부분 수정 후 수용**을 권고합니다.
특히 P0 항목(PAT Cache Miss 인증 우회)은 보고서가 지적한 것보다 심각도가 더 큰 이중 결함(**인증 우회 + 캐시 재적재 후 정당 사용자 락아웃**)이 코드에 존재합니다. 이 항목은 다른 P1~P3 항목보다 먼저, 별도 hotfix로 처리해야 합니다.

| 우선순위 | 항목 | 보고서 타당성 | 조치 형태 |
| :--- | :--- | :--- | :--- |
| **P0** | PAT Cache Miss 인증 우회 | ✅ 타당 (심각도 상향 필요) | Hotfix — 즉시 |
| P1 | X-Forwarded-For Trust Boundary | ✅ 타당 | 운영 오픈 전 필수 |
| P1 | Scope 정책 Fail-Closed | ⚠ 부분 타당 (조건부) | 코드 + 스펙 정비 |
| P1 | Keycloak Token Exchange Down-scoping PoC | ✅ 타당 (RK-01과 정합) | Phase 1 PoC 게이트로 격상 |
| P1 | TXS Network/mTLS 보호 | ✅ 타당 | K8s 이관 시 |
| P2 | JWT Claim Header 신뢰경계 | ✅ 타당 | 계약 문서화 + 표준화 |
| P2 | JWT Cache Scope Hash 축약 | ✅ 타당 (실질 리스크는 낮음) | 저비용 개선 |
| P2 | JWT 권한 변경 Invalidation | ✅ 타당 | 정책 정의 + 훅 추가 |
| P2 | Audit Durable Queue | ✅ 타당 | 단계적 이전 |
| P3 | Quota 계수 기준 | ✅ 타당 (정책 이슈) | 스펙 명문화 |
| P3 | Argon2id/HMAC 문서-코드 정합성 | ✅ 타당 | 설계 문서 개정 |

---

## 1. P0 — PAT Cache Miss 인증 우회 (즉시 수정)

### 1.1 코드 근거

원본 보고서는 "hash가 빈 값이면 HMAC 검증이 수행되지 않는다"고 지적했고, 코드 확인 결과 **의도된 스킵**임이 소스 주석에 명시되어 있습니다.

`services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua:133-136`
```lua
-- Fallback: ask portal-backend for PAT metadata on a Redis cache miss.
-- Mirrors plugins/pat_auth.py's behaviour, including the fact that HMAC
-- verification is skipped for this path (no HMAC ships in the fallback
-- payload — only Redis, populated at issuance time, has it).
```

`services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua:159-171`
```lua
return {
    sub = data.sub,
    scopes = cjson.encode(data.scopes or {}),
    status = data.status,
    cidr = cjson.encode(data.cidr or {}),
    hash = "",         -- 항상 빈 값
    ...
}
```

`services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua:237-243`
```lua
if meta.hash and meta.hash ~= "" then       -- ← hash="" 이면 조건 미진입
    local expected = common.hmac_sha256_hex(conf.server_key, secret)
    if not common.constant_time_eq(expected, meta.hash) then
        ...
        return common.problem_json(...)
    end
end
-- 이후 status/expiry/CIDR/scope만 확인 → 인증 성공
```

결과: **유효한 tokenId + 임의의 43자 secret 조합만으로 인증 통과** (Redis 캐시가 비어 있는 순간에 한해).

### 1.2 이중 결함 — 추가로 발견한 사항

`services/portal-backend/routers/internal.py:68-73` 에서 Python 측 fallback이 Redis에 재적재할 때 `hmac` 필드에 **DB의 SHA256 해시**를 저장합니다.

```python
rc.cache_pat_meta(r, token_id, {
    "hmac": pat["token_hash"],  # ← DB는 SHA256, gateway는 HMAC 기대
    ...
})
```

`services/portal-backend/pat_utils.py:33-51` — DB에는 `SHA256(secret)`이 저장되고, Gateway는 `HMAC-SHA256(server_key, secret)`을 기대합니다. 두 값은 서로 다릅니다.

결과 시퀀스:
1. Redis 비어있는 상태 → 공격자가 유효 tokenId + 임의 secret으로 호출 → **인증 우회 성공**
2. 이 요청 처리 중 Python fallback이 실행되어 Redis에 `hash=<SHA256값>` 재적재
3. **이후 정상 요청**: Redis HIT → HMAC 검증 진입 → `HMAC(server_key, secret) ≠ SHA256(secret)` → **정당 사용자 401**

즉, 이 결함은 **인증 우회**와 **DoS(락아웃)**을 동시에 야기합니다.

### 1.3 보완 방법

두 방향이 있습니다. **방향 B를 권고**합니다.

**방향 A — Fallback을 완전히 제거 (Fail-Closed)**

- `pat-auth.lua`의 `fetch_from_portal` 경로를 삭제하고, Redis 캐시 미스 시 즉시 401 반환.
- 캐시 유실은 Portal 백엔드의 재적재 배치(예: 시작 시 활성 PAT 전량 rehydrate) 및 PAT 발급 시 SETEX로만 채움.
- 장점: Trust Boundary가 단순, 우회 경로 원천 제거.
- 단점: Redis 완전 유실 시 모든 PAT가 재발급될 때까지 서비스 불가.

**방향 B — Fallback을 유지하되 HMAC을 전달·검증 (권고)**

원장(PostgreSQL)에 **HMAC-SHA256(server_key, secret)** 값을 함께 저장하고 fallback 응답에 포함시킵니다.

수정 항목:

1. **DDL 변경** — `cdp.pat` 테이블에 컬럼 추가
   ```sql
   ALTER TABLE cdp.pat ADD COLUMN token_hmac VARCHAR(64);
   -- 기존 PAT는 서비스 재시작 후 다음 사용 시점에 자동 rehydrate 불가하므로
   -- migration 스크립트 없이는 채워지지 않음 → 마이그레이션 정책 별도 결정
   -- (해시 계산이 secret 원문을 필요로 하므로 소급 채움은 불가능)
   -- 방침: 신규 발급분부터 채우고, 소급분은 재발급 유도 또는 배치로 강제 만료
   ```

2. **`services/portal-backend/routers/tokens.py:97-103`** — PAT 발급 시 `token_hmac` 도 함께 INSERT.

3. **`services/portal-backend/routers/internal.py:41-98`** — `/internal/pat/{token_id}` 응답 JSON에 `hmac` 필드 포함. Redis 재적재 코드의 `"hmac": pat["token_hash"]` 를 `"hmac": pat["token_hmac"]` 로 교정.

4. **`services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua`** —
   - `fetch_from_portal` 반환 테이블에 `hash = data.hmac or ""` 로 설정
   - 검증 조건을 뒤집어 **Fail-Closed** 로 변경:
     ```lua
     if not meta.hash or meta.hash == "" then
         common.redis_keepalive(red)
         return common.problem_json(ctx, 401, "CDP-1001",
             "Invalid or revoked token", "hash unavailable for verification")
     end
     local expected = common.hmac_sha256_hex(conf.server_key, secret)
     if not common.constant_time_eq(expected, meta.hash) then
         common.redis_keepalive(red)
         return common.problem_json(ctx, 401, "CDP-1001",
             "Invalid or revoked token", "Token signature mismatch")
     end
     ```
   - 주석의 "HMAC verification is skipped for this path" 문구 삭제.

5. **소급 대응** — 기존에 발급된 PAT는 `token_hmac`이 NULL. 정책 결정 필요:
   - (권고) 배치로 즉시 상태를 `INACTIVE` 전환 + 소유자에게 재발급 안내 메일 (PAT는 발급 시 secret 원문을 폐기했으므로 소급 HMAC 채움 불가)
   - 유예 기간 동안은 NULL 인 경우에 한해 SHA256 fallback 검증을 임시 허용하는 코드를 추가할 수 있으나, 이는 hotfix의 목적을 훼손하므로 비권장

### 1.4 검증 방법

- **회귀 테스트**
  1. Redis flushall → 유효 tokenId + 임의 secret 43자 호출 → 401 확인
  2. Redis flushall → 정상 PAT 호출 → 200 확인 (fallback을 통해 HMAC 검증 통과)
  3. Python fallback이 Redis 재적재한 뒤 두 번째 호출도 200 확인
  4. `token_hmac` 이 NULL 인 (마이그레이션 전) PAT 호출 → 401 확인
- **감사 로그**에 `AUTH_FAILED` + 사유 `hash unavailable for verification` 이벤트 발생 여부 확인

---

## 2. P1 — X-Forwarded-For Trust Boundary

### 2.1 코드 근거

- `services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua:61-70` — XFF 첫 홉을 무조건 신뢰
- `services/token-exchange-service/caller_auth.py:34-40` — 동일 패턴

두 경로 모두 신뢰된 Proxy 여부를 확인하지 않으므로, 외부 클라이언트가 `X-Forwarded-For: 10.0.0.1` 헤더를 임의로 붙이면 CIDR allowlist를 통과할 수 있습니다.

### 2.2 보완 방법

**Gateway (APISIX)**:
1. 배포 시 앞단 L7 LB가 기존 XFF를 반드시 재작성하도록 인프라 설정 (Ingress/NGINX 기준 `real_ip_recursive on; set_real_ip_from <LB CIDR>;`).
2. `pat-auth.lua` 에 `trusted_proxy_cidrs` 설정 필드를 추가하고, `ctx.var.remote_addr` 가 trusted proxy 대역에 속하는 경우에만 XFF를 사용하도록 변경. 그 외에는 `remote_addr` 를 client IP로 사용.
3. `apisix_client._build_route` 에 해당 설정값 전달.

**TXS**:
- TXS는 클러스터 내부 통신이므로 `trusted_proxy_cidrs` 를 서비스 메시/Ingress 대역으로 좁게 지정.
- 궁극적으로는 **§5 (P1 TXS 보호)** 와 함께 mTLS로 대체 예정이므로 XFF 의존 코드 자체를 축소.

**공통**:
- `docs` 또는 `infra/README` 에 "L7 LB의 XFF 재작성이 이 시스템의 IP 검증 정책의 전제조건"임을 명시.

### 2.3 검증
- 외부에서 XFF 스푸핑 요청 → CIDR 위반으로 403 확인
- LB 경유 시 실제 client IP가 감사 로그에 남는지 확인

---

## 3. P1 — Scope 정책 Fail-Closed

### 3.1 코드 근거 및 조건부 판단

보고서는 "Scope mapping이 없으면 허용되는 로직이 위험"이라 지적했습니다. 코드 상태는 다음과 같습니다.

`services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua:47`
```lua
required = { "server_key", "portal_backend_url", "required_scope_map" }
-- required_scope_map 은 schema에서 required, minProperties=1로 강제
```

즉, **Route 등록 시 required_scope_map 자체가 비어 있을 수는 없음** — 여기까지는 이미 Fail-Closed입니다.

다만 **부분 Fail-Open 지점**이 남아 있습니다:

`pat-auth.lua:256-282`
```lua
local method_patterns = conf.required_scope_map[ctx.var.request_method]
...
if method_patterns then
    ...
else
    has_scope = true   -- ← 등록되지 않은 HTTP 메서드는 무조건 허용
end
```

`services/portal-backend/apisix_client.py:79-95` 에서 route에 매핑되는 methods는 실제 등록된 scope로부터 파생되고 OPTIONS만 추가되므로, 이론적으로 `required_scope_map` 에 없는 method가 route까지 도달할 확률은 낮습니다. 그러나 안전측 원칙은 지켜야 합니다.

### 3.2 보완 방법

1. **`pat-auth.lua`의 else 분기 변경** — 메서드 패턴이 없으면 403 반환:
   ```lua
   if method_patterns then
       ...
   else
       -- OPTIONS는 CORS 플러그인이 이미 응답했으므로 여기까지 도달하지 않음.
       -- 그 외에는 정책 부재 = 거부.
       common.redis_keepalive(red)
       return common.problem_json(ctx, 403, "CDP-1003",
           "Required scope not granted",
           "No scope policy for method " .. ctx.var.request_method)
   end
   ```
2. **OPTIONS 예외 처리** — `cors` 플러그인이 pat-auth(prio 3010)보다 앞선(4000)이라 사전 응답되지만, cors 플러그인이 비활성화된 route가 발생할 수 있으므로 `required_scope_map` 에 OPTIONS 항목이 없는 경우에도 403이 반환된다는 사실을 문서화.
3. **catalog 등록 시 검증** — `POST /catalog/apis` 에서 최소 1개 scope 필수, GET/POST/PUT/DELETE 중 정의된 method만 route에 노출되도록 이미 처리됨. 관련 unit test 강화.

### 3.3 검증
- Scope가 정의되지 않은 method(예: PATCH)로 호출 → 403 확인
- Wildcard `/**` scope에 대해 하위 경로가 실제로 커버되는지 확인

---

## 4. P1 — Keycloak Token Exchange Down-scoping PoC

### 4.1 코드 및 문서 근거

`services/token-exchange-service/keycloak_client.py:32-51` 은 `requested_subject`, `audience`, `scope` 를 전달합니다. 이는 **Legacy Token Exchange V1** 문법이며, 아키텍처 문서 `01_시민개발자_API포털_아키텍처정의서.md` RK-01에도 V1이 Preview + Deprecated 상태임이 명시되어 있습니다.

즉 보고서가 지적한 "Down-scoping 실효성 검증 필요"는 **RK-01과 동일한 리스크**로, 이미 아키텍처에서 Phase 1 게이트로 잡혀 있습니다. 다만 현재 코드는 mock Keycloak만 검증되어 실제 Keycloak 26.x 대응이 확인되지 않았습니다.

### 4.2 보완 방법 — 검증 시나리오 5종을 Phase 1 PoC의 필수 합격 기준으로 격상

각 시나리오를 CI에서 반복 가능한 통합 테스트로 작성합니다 (`tests/integration/test_kc_token_exchange.py` 등):

| # | 시나리오 | 기대 결과 |
| :--- | :--- | :--- |
| PoC-01 | 사용자 U는 role A/B/C 보유. PAT는 scope X만 승인. | 교환된 JWT의 scope에 X만 존재 (A/B/C 관련 scope 미포함) |
| PoC-02 | PAT에 없는 scope Y 를 request | 교환 실패 (invalid_scope) or 응답 JWT에 Y 미포함 |
| PoC-03 | requested_subject 를 다른 사용자 V 로 지정 | 교환 실패 (권한 없음) |
| PoC-04 | 허용되지 않은 audience 지정 | 교환 실패 or JWT의 aud claim이 예상값이 아님 |
| PoC-05 | Exchanger client에서 impersonation 권한 제거 후 재시도 | 교환 실패 (403) |

### 4.3 방식 A(V1)와 방식 B(Custom Protocol Mapper) 병행 검증

아키텍처 문서 RK-01가 방식 B(Custom Protocol Mapper + Client Credentials)를 운영 기본으로 잡고 있으므로, 위 5개 시나리오를 **A/B 모두에서** 통과시킨 뒤 최종 확정합니다.

- 방식 B로 전환 시 `keycloak_client.py` 수정 범위:
  - `grant_type` 을 `client_credentials` 로 교체
  - `requested_subject` 삭제, custom mapper가 subject를 주입하도록 client scope 설정에 위임
  - JWT payload의 `sub` 이 실제 사용자 ID 인지, 내부 PDP가 이를 authoritative 하게 인정하는지 별도 확인 필요

### 4.4 결정 마감일
- Phase 1 (2026-10-01~2026-10-31) 종료 전까지 방식 확정. 미결정 시 Phase 2 착수 지연 리스크 있음.

---

## 5. P1 — Token Exchange Service 보호 강화

### 5.1 코드 근거

`services/token-exchange-service/caller_auth.py:1-83` — X-Internal-Key + CIDR 두 층을 사용합니다. 파일 상단 주석이 "container IPs are dynamic in Docker/K8s"라며 CIDR의 한계를 명시하고 있어 이 부분은 이미 인지되어 있습니다.

`services/token-exchange-service/config.py:26` — `allowed_cidr: str = "0.0.0.0/0"` 이 dev 기본값이므로, 배포 시 반드시 좁혀야 합니다.

### 5.2 보완 방법 (K8s 이관 시 확정)

1. **네임스페이스 분리** — TXS를 `citizen-security` 등 별도 네임스페이스로 이동.
2. **NetworkPolicy** — APISIX pod의 `podSelector` 만 TXS의 8080 포트로 접근 허용:
   ```yaml
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata:
     name: allow-apisix-to-txs
   spec:
     podSelector:
       matchLabels: { app: token-exchange-service }
     ingress:
     - from:
       - namespaceSelector: { matchLabels: { name: citizen-gw } }
         podSelector: { matchLabels: { app: apisix } }
       ports:
       - protocol: TCP
         port: 8080
   ```
3. **mTLS 또는 Service Identity** — Istio/Linkerd 도입 시 mTLS로 자동 대체. 도입 전에는 X-Internal-Key를 **HashiCorp Vault** 또는 K8s Secret + CSI Secret Store로 관리하고, 회전 정책 수립.
4. **X-Internal-Key 회전 절차 문서화** — 무중단 회전 시나리오 (Redis에 두 개 키 잠시 병존 → 순차 교체).
5. **caller_auth.py 개선** — `settings.internal_api_key` 가 비어 있으면 프로세스 자체를 기동 실패로 처리 (현재는 경고 로그만 남김).

---

## 6. P2 — JWT Claim Header의 신뢰경계

### 6.1 코드 근거

`services/apisix-gateway/plugins/apisix/plugins/pat-token-exchange.lua:82-100`
- Gateway가 JWT payload를 서명 검증 없이 decode하여 `X-USER-ID` 등 헤더로 주입.
- 이후 Internal Gateway가 JWT 서명을 검증하므로 **최종 인가는 JWT 기반**이지만, 다운스트림 서비스가 헤더를 직접 신뢰할 경우 위험.

`services/apisix-gateway/plugins/apisix/plugins/pat-token-exchange.lua:66-76`
- `strip_spoofable_headers` 가 클라이언트 임의 헤더를 제거하는 것은 좋음.

### 6.2 보완 방법

1. **Trust Boundary 계약 문서화** — `docs/security_headers.md` 신설, 다음을 명시:
   - `X-USER-ID` 등 헤더는 **Context** 이지 **Authority** 가 아님
   - 도메인 서비스는 인가 결정을 **JWT의 검증된 claim** 또는 **Internal Gateway가 재발행한 신뢰 헤더**로만 수행
   - 편의상 헤더를 참조하더라도, 헤더와 JWT claim이 다르면 **JWT 우선**
2. **(장기) Internal Gateway가 헤더 재생성** — Internal Gateway가 JWT 서명 검증 후 신뢰 헤더를 재주입하는 방식으로 이관. Citizen Gateway에서는 헤더 주입 로직 제거.
3. **단기 완화** — Citizen Gateway가 주입하는 헤더 이름에 `X-CDP-*` 접두어를 붙여, 도메인 서비스가 이를 참조하지 않도록 표시. 기존 `X-USER-ID` 등은 Internal Gateway 통과 후에만 유효.

---

## 7. P2 — JWT Cache Scope Hash 축약

### 7.1 코드 근거

`services/token-exchange-service/cache.py:18-19`
```python
def _scope_hash(scopes: list[str]) -> str:
    return hashlib.sha256(" ".join(sorted(scopes)).encode()).hexdigest()[:8]
```

`services/apisix-gateway/plugins/apisix/plugins/pat-token-exchange.lua:36-43` 도 동일하게 8자로 축약.

실질 리스크: **같은 tokenId 안에서만** 충돌하므로 (`cdp:jwt:{tokenId}:{scopeHash}` 구조), 한 사용자의 서로 다른 scope 조합이 32bit 안에서 충돌해야 문제 발생. 확률적으로 낮지만 0은 아님.

### 7.2 보완 방법

1. **hash 길이 확장** — 8자 → 16자(64bit) 이상으로 확장. Redis 키 길이 영향은 무시할 수 있음.
2. Lua와 Python 양쪽 동시 수정 필요 (같은 hash를 계산해야 캐시 일관성 유지).

수정 지점:
- `cache.py:19` `[:8]` → `[:16]`
- `pat-token-exchange.lua:42` `common.sha256_hex(...):sub(1, 8)` → `sub(1, 16)`

캐시 무효화 절차: 배포 시 기존 캐시는 자동 만료(240s TTL). 별도 조치 불필요.

---

## 8. P2 — JWT 권한 변경 Invalidation

### 8.1 현재 정책

- JWT Cache TTL = 240s (설정: `settings.jwt_cache_ttl`)
- PAT 폐기 시 `services/portal-backend/redis_client.py:invalidate_pat` 가 `cdp:jwtidx:{token_id}` set을 순회하여 관련 JWT 캐시를 모두 삭제 → **PAT 폐기는 즉시 반영**
- Quota 변경도 `update_pat_quota_in_redis` 로 즉시 반영 (PAT 메타만)
- 그러나 **다음 이벤트에는 hook이 없음**:
  - Keycloak Role 변경
  - PAT의 granted_scopes 변경 (현재는 API 미노출)
  - API의 Scope 정의 변경
  - 사용자 비활성화 (Keycloak Disabled)

### 8.2 보완 방법

1. **정책 문서화** — 각 이벤트별 최대 반영 시간을 명시:
   | 이벤트 | 반영 시점 | 방법 |
   | :--- | :--- | :--- |
   | PAT 폐기 | 즉시 | invalidate_pat |
   | Quota 변경 | 즉시 | update_pat_quota_in_redis |
   | Keycloak Role 변경 | 최대 240초 | JWT Cache TTL 만료 |
   | 사용자 Disable | 즉시 (권고) | Keycloak Event Listener → Portal → invalidate_pat |
   | API Scope 재정의 | 즉시 (권고) | catalog 갱신 시 관련 tokenId JWT 캐시 flush |
2. **Keycloak Event Listener 추가** — LOGOUT/USER_DELETE 이벤트를 Portal에 webhook으로 전달하고, 해당 user의 모든 PAT를 즉시 invalidate.
3. **catalog 갱신 훅** — `PATCH /catalog/apis/{apiId}` 및 scope 편집 API에서 해당 API를 사용하는 모든 tokenId의 JWT 캐시를 flush.

---

## 9. P2 — 보안 Audit의 Durable 처리

### 9.1 코드 근거

`services/apisix-gateway/plugins/apisix/plugins/pat-audit.lua:38-108`
- Worker-local 배치 + 주기 flush + 종료 시 best-effort 시도
- HTTP flush 실패 시 이벤트 로그만 남기고 데이터는 유실 (line 58)

정상 API 호출(`API_CALL`)은 유실되어도 통계 왜곡 수준이나, `AUTH_FAILED`, `SCOPE_DENIED`, `EXCHANGE_FAILED` 는 침해 분석에 필수이므로 유실이 허용되지 않습니다.

### 9.2 보완 방법 (단계적)

**Phase 1 — Redis Stream 브릿지 (기존 인프라 재사용)**
1. `pat-audit.lua` 를 두 갈래로 분기:
   - `event_type ∈ {AUTH_FAILED, SCOPE_DENIED, EXCHANGE_FAILED, QUOTA_EXCEEDED}` → `XADD cdp:audit:security *` (Redis Stream)
   - 나머지 (`API_CALL`) → 기존 HTTP 배치 유지
2. Portal 백엔드에 `services/portal-backend/audit_consumer.py` 신설:
   - `XREADGROUP` 으로 consumer group 소비
   - PostgreSQL `cdp.audit_log` 에 INSERT 후 XACK
3. Redis AOF everysec 설정 확인.

**Phase 2 — 운영 SIEM 이관**
- Redis Stream → Kafka Connect 또는 별도 shipper 로 SIEM 전송.
- 감사 로그 보존 정책 (1년) 는 SIEM 측에서 관리.

**중간 대책 (즉시 적용 가능)**
- `pat-audit.lua:57-61` 에서 flush 실패 시 `_batch` 앞에 되돌리는 재큐잉을 추가하고, 최대 buffer 크기 초과 시에만 drop. (단, worker 재시작에는 여전히 취약)

---

## 10. P3 — Quota 계수 기준 명문화

### 10.1 현재 동작

`pat-quota.lua` 는 pat-auth 통과 후 → token exchange 전에 원자적으로 차감합니다. 따라서 다음 두 케이스도 quota 1회 차감됩니다:
- Token Exchange 실패 (IdP 장애 등)
- Internal Gateway/PDP가 DENY 반환
- Upstream 5xx

### 10.2 보완 방법

**정책 옵션 선택 후 스펙에 명문화**:

| 옵션 | 정의 | 장점 | 단점 |
| :--- | :--- | :--- | :--- |
| A (현재) | 인증·스코프 통과 시 차감 | 단순, 원자적 | 정상 사용자가 IdP/PDP 장애로 quota 소진 |
| B | Upstream 2xx만 차감 | 사용자 관점 공정 | log phase에서 refund 필요, 원자성 저하 |
| C | 5xx만 refund | 절충안 | 관리 복잡 |

**권고: A + 5xx refund (옵션 C)**
- `pat-audit.lua` 또는 별도 `pat-quota-refund.lua` 를 log phase에 추가
- status_code >= 500 이면서 error_code가 `CDP-2001/CDP-2002` 인 경우 `INCRBY cdp:quota:d:{tokenId}:{yyyymmdd} -1` 로 되돌림
- 429/401/403은 차감 유지 (사용자 책임)

**스펙 문서 수정** — `02_시민개발자_API포털_구현상세스펙.md` §2.3 (쿼터 차감 Lua 스크립트) 및 §4.3 (오류 코드) 절에 계수 정책 표를 추가.

---

## 11. P3 — 설계문서 Argon2id/HMAC 정합성

### 11.1 불일치 확인

| 문서 | 코드 |
| :--- | :--- |
| 아키텍처 정의서 R-07: "PAT 해시 저장(Argon2id)" | `pat_utils.py:33` — `hash_secret_sha256` 사용 |
| 구현 상세스펙 §3.2: Argon2id (m=19MiB, t=2, p=1) | 실제 DB `token_hash` = SHA256 |
| 구현 상세스펙 §3.2 주석: Redis에 HMAC-SHA256 저장 | 실제 저장됨 (issue 시점) |

`pat_utils.py:33-43` 는 명시적으로 SHA256을 선택한 이유(secret이 256bit CSPRNG이라 Argon2id 필요 없음)를 주석으로 남겼습니다. 이 설계 판단은 합리적이나 **문서와 상충**합니다.

### 11.2 보완 방법

**설계 문서를 실제 구현에 맞춰 개정** (구현이 더 타당한 근거를 가지고 있으므로):

1. **`01_시민개발자_API포털_아키텍처정의서.md` R-07** 수정:
   ```
   R-07 보안: PAT secret은 256bit CSPRNG로 생성되므로 원장(PostgreSQL)에는
   SHA-256 해시로 저장하고, Gateway 검증용으로 Redis에 HMAC-SHA256(server_key)
   사전계산값을 함께 보관. 평문 미보관.
   ```
2. **`02_시민개발자_API포털_구현상세스펙.md` §3.2** 갱신:
   - Argon2id 파라미터 표 삭제
   - "PAT secret 원문은 256bit CSPRNG이므로 Argon2id 등 memory-hard 해시 불필요. SHA256 + HMAC 2단 캐시로 충분" 근거 명시
   - **DDL 스펙 §2.1.4** — `token_hash VARCHAR(255)` 를 SHA256 hex 기준 `VARCHAR(64)`로 정정
   - §1의 P0 보완이 반영되면 `token_hmac VARCHAR(64)` 추가도 함께 반영
3. **§2.2 Redis 키 설계** — `cdp:pat:{tokenId}` hash 필드 목록에 `hash` (HMAC hex) 명시적으로 기재.

---

## 12. 실행 순서 및 마일스톤

### 12.1 즉시 (D+0 ~ D+3, Hotfix)
- [P0] §1 — PAT Cache Miss 인증 우회 수정 (DDL + Lua + Python 동시 배포)
- [P3-문서] §11 — 문서와 코드 정합성 확보 (P0과 함께 반영해야 리뷰 혼선 방지)

### 12.2 Phase 1 PoC 게이트 (~2026-10-31)
- [P1] §4 — Keycloak Token Exchange Down-scoping 5개 시나리오 통과 (방식 A/B 병행)
- [P1] §2 — XFF Trust Boundary 인프라 표준 확정 및 문서화
- [P1] §3 — Scope Fail-Closed 코드 반영 (else 분기 403)

### 12.3 Phase 2/3 개발 중 (2026-11-15 ~ 2027-02-19)
- [P1] §5 — TXS NetworkPolicy 적용, X-Internal-Key 회전 절차 수립
- [P2] §6 — JWT Claim Header 계약 문서화, X-CDP-* 접두어 도입
- [P2] §7 — Scope Hash 8→16자
- [P2] §8 — Keycloak Event Listener + catalog 갱신 훅
- [P2] §9 — Audit Redis Stream 이관 Phase 1
- [P3] §10 — Quota 계수 정책 확정 및 refund 로직

### 12.4 오픈 이후 (2027-02-20~)
- §5 — mTLS/Service Identity 도입
- §9 — Audit Phase 2 (SIEM)

---

## 13. 회귀 방지

1. **테스트 추가** — 각 P0/P1 시나리오에 대한 통합 테스트를 `tests/security/` 하위에 필수 등록.
2. **CI 게이트** — P0 시나리오는 CI에서 실패 시 머지 차단.
3. **Threat Model 문서** — `docs/threat_model.md` 신설, 위 P0/P1 시나리오를 STRIDE 표에 반영.
4. **연 1회 보안 재점검** — 아키텍처 정의서 §8.1 STRIDE 표에 위 항목을 통합하여 정기 리뷰 항목으로 등록.
