# Citizen Developer API Portal — 구현 계획

**기반 문서**: SA-ARCH-CDP-001, SA-SPEC-CDP-002  
**구현 범위**: Zone 1 (클라이언트), Zone 2 (DMZ 레이어 전체) / Zone 3·4는 Mock  
**구현 언어**: Python/FastAPI (Spring Boot 스펙과 기능 동일, 빠른 구현·검증 목적)  
**평가 완료**: 2회 (2026-09-19)

---

## 핵심 설계 불변 규칙 (전 단계 적용)
1. PAT 평문은 발급 응답 1회 외 어떤 스토어/로그에도 저장 금지
2. Redis 키: `cdp:pat:{tokenId}` → HMAC 값 저장 (Argon2id는 DB만)
3. Lua 쿼터 스크립트가 원자성 보장 — Python fallback 아님
4. JWT 캐시 삭제 시 `cdp:jwtidx:{tokenId}` Set 역색인으로 처리 (KEYS/SCAN 금지)
5. 전체 오류 응답은 RFC 9457 `application/problem+json` 포맷

---

## Phase 0 — 프로젝트 구조 및 인프라 설정

- [x] **0.1** 디렉토리 구조 생성 및 `.env` 파일 작성
- [x] **0.2** `docker-compose.yml` 작성 (postgres, redis, mock-keycloak, mock-internal-gw, token-exchange-service, portal-backend, citizen-gateway, portal-web)
- [x] **0.3** `infra/postgres/init/01_ddl.sql` — 스펙 2.1절 DDL 전체 (cdp 스키마, 7개 테이블, 파티션, 인덱스)
- [x] **0.4** `infra/postgres/init/02_seed.sql` — API 카탈로그 3종, 승인된 신청 1건 (테스트용)
- [x] **0.5** `infra/redis/redis.conf` — requirepass, AOF, maxmemory 설정
- [x] **0.6** `infra/redis/quota_deduct.lua` — 스펙 2.3절 정확한 Lua 스크립트
- [x] **0.7** `infra/redis/unlock_if_mine.lua` — 단일비행 CAS 언락 스크립트

---

## Phase 1 — Mock Zone 3 서비스

### 1.1 Mock Keycloak (실제 RSA 서명 JWT 발급)

- [x] **1.1.1** `services/mock-keycloak/Dockerfile` + `requirements.txt`
- [x] **1.1.2** `services/mock-keycloak/certs/gen_keys.py` — RSA 2048 키쌍 + JWKS 생성
- [x] **1.1.3** `services/mock-keycloak/main.py`:
  - `GET /.well-known/openid-configuration`
  - `GET /realms/hd/protocol/openid-connect/certs` (JWKS)
  - `POST /realms/hd/protocol/openid-connect/token`:
    - `client_credentials` grant (서비스 계정용)
    - `token-exchange` grant (RFC 8693, `requested_subject` 임퍼소네이션)
    - 발급 JWT에 `aud=["internal-api-gateway"]`, `scope`, `cdp_channel="citizen"`, `cdp_pat_id`, RS256 서명
  - `GET /admin/realms/hd/users/{user_sub}` (역할 조회)
- [x] **1.1.4** Mock 사용자 레지스트리: `u-test-001`(citizen-developer, mdm-reader), `u-test-002`(api-owner), `u-admin-001`(platform-admin)

### 1.2 Mock Internal Gateway + PDP + MSA

- [x] **1.2.1** `services/mock-internal-gw/Dockerfile` + `requirements.txt`
- [x] **1.2.2** `services/mock-internal-gw/main.py`:
  - JWT 검증 미들웨어 (JWKS 조회, 서명·exp·aud·iss 검증)
  - PDP 내장: 유효 JWT → 항상 PERMIT
  - Mock API: `GET /internal/api/v1/vendors`, `POST /internal/api/v1/vendors`, `GET /internal/api/v1/orders`, `GET /internal/api/v1/employees`
  - 헤더 로깅: `X-Citizen-PAT-Id`, `X-Citizen-Channel`, `X-Request-Id`
- [x] **1.2.3** `services/mock-internal-gw/jwks_cache.py` — 5분 TTL 인메모리 캐시

---

## Phase 2 — Token Exchange Service (TXS)

- [x] **2.1** `services/token-exchange-service/Dockerfile` + `requirements.txt`
- [x] **2.2** `services/token-exchange-service/config.py` — Pydantic Settings (Keycloak URL, Redis, 서킷브레이커 파라미터)
- [x] **2.3** `services/token-exchange-service/redis_client.py` — 연결 풀, Lua 스크립트 로드
- [x] **2.4** `services/token-exchange-service/keycloak_client.py`:
  - `get_exchanger_token()` — client_credentials, 250초 캐시
  - `exchange_token()` — RFC 8693 token-exchange 호출
- [x] **2.5** `services/token-exchange-service/circuit_breaker.py`:
  - Redis `cdp:cb:txs` (SETEX 30 "OPEN")로 서킷 상태 관리
  - 슬라이딩 윈도우 50건, 실패율 50% 임계값
- [x] **2.6** `services/token-exchange-service/cache.py` — JWT 캐시 + 단일비행:
  - scopeHash = sha256(sorted scopes)[:8]
  - Redis GET `cdp:jwt:{tokenId}:{scopeHash}`
  - 확률적 조기 갱신 (TTL < 48초 시)
  - `SET NX PX 5000` 락 → 교환 → SETEX 240 + SADD jwtidx
  - 락 실패 시 50ms × 6회 폴링
- [x] **2.7** `services/token-exchange-service/main.py`:
  - `POST /internal/token-exchange` — IP 화이트리스트 검증
  - `GET /health` — 서킷 상태 포함
  - `GET /metrics` — Prometheus 포맷
- [x] **2.8** `services/token-exchange-service/metrics.py` — `cdp_token_exchange_total`, `cdp_token_exchange_latency_seconds`

---

## Phase 3 — Portal Backend API

- [x] **3.1** `services/portal-backend/Dockerfile` + `requirements.txt`
- [x] **3.2** `services/portal-backend/config.py` — Pydantic Settings
- [x] **3.3** `services/portal-backend/database.py` — asyncpg 연결 풀
- [x] **3.4** `services/portal-backend/pat_utils.py`:
  - `generate_token_id()` — base62 12자
  - `generate_secret()` — 32바이트 CSPRNG → base64url 43자
  - `hash_secret()` — Argon2id (m=19456KB, t=2, p=1, len=32)
  - `compute_hmac()` — HMAC-SHA256(secret, SERVER_KEY) → hex (게이트웨이용)
- [x] **3.5** `services/portal-backend/redis_client.py` — PAT 메타 캐시 CRUD, JWT 색인 삭제
- [x] **3.6** `services/portal-backend/auth.py` — Bearer JWT 검증, 사용자 클레임 추출
- [x] **3.7** `services/portal-backend/error_handlers.py` — RFC 9457 `application/problem+json`
- [x] **3.8** `services/portal-backend/routers/catalog.py`:
  - `GET/POST /portal/v1/catalog/apis` + `GET/PATCH /portal/v1/catalog/apis/{apiId}`
  - 등록 파이프라인: 검증 → DB INSERT(DRAFT) → Gateway 라우트 생성 → 스모크 테스트 → PUBLISHED or 롤백
- [x] **3.9** `services/portal-backend/routers/applications.py`:
  - `POST /portal/v1/applications` — 자격 사전검증 (IdP 역할 조회)
  - `GET /portal/v1/applications` — 본인/결재 대상 목록
  - `PATCH /portal/v1/applications/{appId}` — 승인/반려
- [x] **3.10** `services/portal-backend/routers/tokens.py`:
  - `POST /portal/v1/tokens` — PAT 발급 (신청당 3개 / 사용자당 10개 제한, 1회 노출)
  - `GET /portal/v1/tokens` — 메타 목록 (해시·평문 제외)
  - `DELETE /portal/v1/tokens/{tokenId}` — 폐기 + Redis 즉시 삭제
  - `PUT /portal/v1/tokens/{tokenId}/quota` — 쿼터 변경 + Redis 즉시 반영
- [x] **3.11** `services/portal-backend/routers/usage.py` — 일별 통계 + 실시간 Redis 쿼터 카운터
- [x] **3.12** `services/portal-backend/routers/audit.py` — 감사 로그 페이징 조회
- [x] **3.13** `services/portal-backend/routers/internal.py`:
  - `GET /internal/pat/{tokenId}` — 게이트웨이 캐시 미스 시 폴백
  - `POST /internal/audit` — 비동기 감사 배치 수신
- [x] **3.14** `services/portal-backend/routers/me.py` — `GET /portal/v1/me`
- [x] **3.15** `services/portal-backend/batch.py` — APScheduler: BAT-CDP-01~05
- [x] **3.16** `services/portal-backend/main.py` — 앱 조립, 라우터 등록, 스케줄러 시작

---

## Phase 4 — Citizen Gateway (Python FastAPI 프록시)

APISIX 대신 Python FastAPI로 동일한 게이트웨이 로직 구현 (PAT 인증 + 쿼터 + 토큰 교환 + 프록시).

- [x] **4.1** `services/citizen-gateway/Dockerfile` + `requirements.txt`
- [x] **4.2** `services/citizen-gateway/config.py` + `services/citizen-gateway/redis_client.py`
- [x] **4.3** `services/citizen-gateway/plugins/pat_auth.py` — 스펙 6.2절:
  - PAT 포맷 파싱 (hdpat_<12>_<43>)
  - Redis HGETALL `cdp:pat:{tokenId}` → 캐시 미스 시 portal-backend 조회
  - Negative Cache (30초, 미존재 tokenId)
  - HMAC-SHA256 상수시간 비교 (hmac.compare_digest)
  - 허용 CIDR 검증
  - Scope 매칭 (METHOD:PATH ↔ required_scope_map)
- [x] **4.4** `services/citizen-gateway/plugins/quota.py` — 스펙 6.3절:
  - Redis `EVALSHA quota_deduct.lua` (원자적 실행)
  - 반환: `{0,"RATE_LIMIT",0}` → 429 CDP-1004 + `Retry-After`
  - 성공 시 `X-RateLimit-*` 헤더 주입
- [x] **4.5** `services/citizen-gateway/plugins/token_exchange.py` — 스펙 6.4절:
  - Redis JWT 캐시 조회
  - TXS `/internal/token-exchange` 호출
  - `Authorization: Bearer <JWT>` 헤더 치환
  - 부가 헤더 주입: `X-Citizen-PAT-Id`, `X-Citizen-Channel`, `X-Request-Id`
  - 클라이언트 제공 위험 헤더 제거: `X-Citizen-*`, `X-Forwarded-User`, `X-User-Sub`, `Cookie`
- [x] **4.6** `services/citizen-gateway/plugins/audit.py` — 로그 배치 전송 (스펙 6.5절, PAT 평문 마스킹)
- [x] **4.7** `services/citizen-gateway/proxy.py` — httpx 역방향 프록시 (헤더 처리, 스트리밍)
- [x] **4.8** `services/citizen-gateway/scope_config.py` — 라우트별 required_scope_map 설정
- [x] **4.9** `services/citizen-gateway/main.py` — 플러그인 체인 조립, 라우트 매핑

---

## Phase 5 — Frontend (Vue 3 SPA, 최소화)

- [x] **5.1** `frontend/portal-web/` Vue 3 프로젝트 초기화 (Vite, TypeScript, Pinia, Vue Router, Axios)
- [x] **5.2** `src/router/index.ts` — 10개 라우트 (스펙 7.3절), 인증 가드
- [x] **5.3** `src/stores/` — auth, catalog, application, token (평문 토큰 스토어 저장 금지)
- [x] **5.4** `src/views/CatalogList.vue` — API 카탈로그 목록, 검색
- [x] **5.5** `src/views/ApiDetail.vue` — API 상세, Swagger UI 뷰어
- [x] **5.6** `src/views/ApplicationForm.vue` — 사용 신청 폼, 자격 배지
- [x] **5.7** `src/views/MyApplications.vue` + `ApprovalInbox.vue`
- [x] **5.8** `src/views/TokenList.vue` + `TokenIssueDialog.vue` — 스펙 7.4절 보안 UX 7개 요건 전체 구현
- [x] **5.9** `src/views/UsageDashboard.vue` — ECharts 호출 트렌드 + 쿼터 게이지
- [x] **5.10** `src/views/AuditLogTable.vue` (관리자)
- [x] **5.11** `frontend/portal-web/Dockerfile` + `nginx.conf` (BFF 역방향 프록시)

---

## Phase 6 — Zone 1 클라이언트 스크립트

- [x] **6.1** `clients/curl_examples.sh` — 스펙 부록 A.1 cURL 예제 5종
- [x] **6.2** `clients/python_client/api_client.py` — CitizenApiClient 클래스 (429 Retry-After 처리 포함)
- [x] **6.3** `clients/python_client/examples/example_normal_call.py`
- [x] **6.4** `clients/python_client/examples/example_rate_limit.py`
- [x] **6.5** `clients/python_client/examples/example_portal_workflow.py` — 카탈로그 조회→신청→승인→PAT 발급→API 호출 전체 흐름

---

## Phase 7 — 통합 테스트

- [x] **7.1** `tests/requirements.txt` (pytest, pytest-asyncio, httpx, redis, python-jose, freezegun)
- [x] **7.2** `tests/conftest.py` — fixture: portal_client, gateway_client, redis_client, valid_pat, revoked_pat
- [x] **7.3** `tests/integration/test_tc_a.py` — 인증/인가 테스트:
  - TC-A-01: 유효 PAT + 허용 경로 → 200 + 쿼터 헤더
  - TC-A-02: 폐기 PAT → 401 CDP-1001 + Redis 키 부재 확인
  - TC-A-03: 만료 PAT (Redis expires_at 조작) → 401 CDP-1001
  - TC-A-04: Scope 외 경로 → 403 CDP-1003
  - TC-A-05: 허용 CIDR 위반 → 403 CDP-1006
- [x] **7.4** `tests/integration/test_tc_q.py` — 쿼터/Rate Limit 테스트:
  - TC-Q-01: 동시 15 req → 429 + Retry-After 확인
  - TC-Q-02: Redis 카운터 4999 주입 후 1건 성공, 다음 429
  - TC-Q-03: 카운터 삭제(자정 리셋 시뮬레이션) → 정상 복귀
- [x] **7.5** `tests/integration/test_tc_x.py` — 토큰 교환 테스트:
  - TC-X-01: JWT 캐시 사전 적재 → 200 (캐시 히트)
  - TC-X-02: 캐시 없음 + 서킷 OPEN → 503 CDP-2001
  - TC-X-03: TXS 직접 호출 → JWT claims (aud, scope, sub, cdp_channel) 검증
- [x] **7.6** `tests/integration/test_tc_p.py` — 포털 PAT 관리 테스트:
  - TC-P-01: 미승인 신청으로 PAT 발급 → 403 CDP-4003
  - TC-P-02: 4번째 PAT 발급 → 409 CDP-4009
  - TC-P-03: 폐기 직후 게이트웨이 호출 → 401 (즉시 반영)
- [x] **7.7** `tests/integration/test_full_workflow.py` — 전체 E2E 플로우
- [x] **7.8** `tests/security/test_security.py`:
  - PAT 평문 감사로그 노출 0건 확인
  - 헤더 스푸핑 차단 확인 (X-Citizen-PAT-Id 위조)
  - Redis 원자 쿼터 정확성 (동시 20건, 정확히 10건 성공)
  - Negative Cache 동작 확인 (존재하지 않는 tokenId)
- [x] **7.9** `tests/README.md` — 실행 방법

---

## Phase 8 — 관측성 (최소)

- [x] **8.1** `infra/prometheus/prometheus.yml` — portal-backend, TXS, citizen-gateway 스크레이프 설정
- [x] **8.2** portal-backend에 `cdp_active_pat_count`, `cdp_api_requests_total` 지표 추가
- [x] **8.3** `infra/grafana/dashboards/cdp_overview.json` — 요청률, 교환 히트율, PAT 수, 쿼터 거부율 패널

---

## 실행 순서

```bash
# 1. 인프라 기동
docker-compose up -d postgres redis

# 2. Zone 3 Mock 기동
docker-compose up -d mock-keycloak mock-internal-gw

# 3. Zone 2 서비스 기동
docker-compose up -d token-exchange-service portal-backend citizen-gateway

# 4. Frontend 기동
docker-compose up -d portal-web

# 5. 통합 테스트 실행
cd tests && pip install -r requirements.txt && pytest integration/ -v && pytest security/ -v
```

---

## 테스트 실행 결과 (2026-09-19)

```
19 passed in 6.21s
```

| 단계 | 결과 |
|------|------|
| 서비스 기동 | 10/10 컨테이너 정상 (postgres, redis, mock-keycloak, mock-internal-gw, token-exchange-service, portal-backend, citizen-gateway, portal-web, prometheus, grafana) |
| 통합 테스트 | **15/15 PASSED** (TC-A×5, TC-P×3, TC-Q×3, TC-X×3, E2E×1) |
| 보안 테스트 | **4/4 PASSED** (SEC-01~04) |
| 전체 | **19/19 PASSED** |

---

## TC 매핑 요약

| TC ID | 파일 | 합격 기준 |
|-------|------|----------|
| TC-A-01 | test_tc_a.py | 200 + X-RateLimit-* 헤더 |
| TC-A-02 | test_tc_a.py | 401 CDP-1001, Redis 키 삭제 확인 |
| TC-A-03 | test_tc_a.py | 401 CDP-1001 |
| TC-A-04 | test_tc_a.py | 403 CDP-1003 |
| TC-A-05 | test_tc_a.py | 403 CDP-1006 |
| TC-Q-01 | test_tc_q.py | 429 + Retry-After |
| TC-Q-02 | test_tc_q.py | 429 CDP-1004 |
| TC-Q-03 | test_tc_q.py | 200 (리셋 후 정상) |
| TC-X-01 | test_tc_x.py | 200 (캐시 히트) |
| TC-X-02 | test_tc_x.py | 503 CDP-2001 |
| TC-X-03 | test_tc_x.py | aud/scope/sub/cdp_channel 정확 |
| TC-P-01 | test_tc_p.py | 403 CDP-4003 |
| TC-P-02 | test_tc_p.py | 409 CDP-4009 |
| TC-P-03 | test_tc_p.py | 401 즉시 반영 |
| SEC-01 | test_security.py | PAT 평문 0건 |
| SEC-02 | test_security.py | 스푸핑 차단 |
| SEC-03 | test_security.py | 원자 카운터 정확성 |
| SEC-04 | test_security.py | Negative Cache 동작 |
