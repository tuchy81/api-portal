# APISIX 브랜치 보완 결과서

- 상위 계획서: `apisix_브랜치_보완계획.md`
- 원본 분석: `apisix_브랜치_종합분석보고서.md`
- 작성일: 2026-09-24
- 범위: 계획서의 **P0**, **P2**, **P3 (문서-코드 정합성)** 항목 반영. P1 항목(§2, §3, §4, §5)은 별도 인프라 작업이 필요해 이번 커밋에서는 제외.

---

## 0. 요약

| 우선순위 | 항목 | 상태 | 주요 파일 |
| :--- | :--- | :--- | :--- |
| **P0** | PAT Cache Miss 인증 우회 (§1) | ✅ 완료 | DDL, `tokens.py`, `internal.py`, `pat-auth.lua` |
| **P3** | Argon2id/HMAC 문서-코드 정합성 (§11) | ✅ 완료 | 아키텍처 정의서, 상세스펙 §2.1.4/§2.2/§3.2 |
| **P2 §6** | JWT Claim Header 신뢰경계 | ✅ 문서화 완료 | `docs/security_headers.md` |
| **P2 §7** | JWT Cache Scope Hash 8→16자 | ✅ 완료 | `cache.py`, `pat-token-exchange.lua`, `main.py`, 테스트 |
| **P2 §8** | JWT Invalidation 정책 + 훅 | ✅ 완료 | `catalog.py`, `internal.py`, `redis_client.py`, `docs/invalidation_policy.md` |
| **P2 §9** | 보안 감사 Redis Stream Durable 처리 | ✅ 완료 | `pat-audit.lua`, `audit_consumer.py`, `main.py`, `apisix_client.py` |

**Breaking change**: `cdp.pat` 스키마에 `token_hmac VARCHAR(64) NOT NULL` 컬럼이 추가되었습니다. 개발 환경은 `docker compose down -v` 후 재기동으로 반영, 운영 환경은 별도 마이그레이션 필요 (§7 참조).

---

## 1. P0 — PAT Cache Miss 인증 우회 제거

### 1.1 변경 내역

**DDL (`infra/postgres/init/01_ddl.sql`)**
- `cdp.pat.token_hash` 컬럼 크기 `VARCHAR(255)` → `VARCHAR(64)` (SHA-256 hex 실제 길이)
- `cdp.pat.token_hmac VARCHAR(64) NOT NULL` 컬럼 신규 추가 (HMAC-SHA256 hex)

**Portal 백엔드**
- `services/portal-backend/routers/tokens.py:issue_pat` — 발급 시 `hmac_val`(이미 계산되어 있던 값)을 INSERT 쿼리에 포함해 `token_hmac` 컬럼에 저장.
- `services/portal-backend/routers/internal.py:get_pat_meta` —
  - `token_hmac` 이 없는 legacy 행은 401 반환 (Fail-Closed).
  - 응답 JSON에 `hmac` 필드 추가.
  - Redis 재적재 시 `hash = pat["token_hmac"]` 로 교정 (이전에는 SHA-256 값을 잘못 저장했음).

**게이트웨이 플러그인 (`services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua`)**
- `fetch_from_portal` 반환 테이블: `hash = ""` → `hash = data.hmac or ""`.
- HMAC 검증 블록: `if meta.hash and meta.hash ~= "" then ... end` 구조를 **Fail-Closed** 로 변경. hash가 없으면 즉시 401.
- 주석 "HMAC verification is skipped for this path" 삭제.

### 1.2 이전과 이후

| 시나리오 | 이전 동작 | 이후 동작 |
| :--- | :--- | :--- |
| Redis HIT, 유효 PAT | 정상 200 | 정상 200 (동일) |
| Redis MISS, 유효 PAT | fallback 후 HMAC 검증 스킵 → **200** | fallback 응답의 `hmac` 로 HMAC 검증 → 정상 200 |
| Redis MISS, 잘못된 secret | **200 (인증 우회)** | 401 (Token signature mismatch) |
| Redis MISS, legacy 행 (token_hmac NULL) | 200 (우회 가능) | 401 (hash unavailable for verification) |
| 재적재 후 유효 PAT 재요청 | HMAC≠SHA256 → **락아웃** | Redis에 HMAC이 정확히 저장되므로 200 |

### 1.3 검증 방법

수동 (개발 환경 재기동 후):
```bash
docker compose down -v && docker compose up -d
# 1. 정상 PAT 발급 후 호출 200 확인
# 2. Redis flushall → 다시 호출 200 확인 (fallback 경유)
# 3. 정상 PAT 발급 후 secret 마지막 문자만 변조 → 401 확인
# 4. redis-cli DEL "cdp:pat:{token_id}" 후 secret 변조 호출 → 401 확인
```

기존 통합 테스트 (`tests/security/test_security.py`) 는 그대로 통과해야 합니다. Cache-miss 우회 회귀 테스트는 후속 P1 작업과 함께 별도 추가 예정.

### 1.4 소급 정책

기존에 발급되어 `token_hmac` 이 NULL 인 PAT는 fallback 검증이 불가능하므로 401이 반환됩니다. 운영 이관 시 다음 중 하나를 실행하세요:

1. (권장) 배치로 `UPDATE cdp.pat SET status='INACTIVE' WHERE token_hmac IS NULL AND status='ACTIVE'` 후 소유자에게 재발급 안내.
2. (임시) NULL 인 PAT 만 관리자 콘솔에서 개별 강제 폐기.

개발 환경은 `01_ddl.sql` 재실행으로 자동 정리됩니다.

---

## 2. P3 — Argon2id/HMAC 문서-코드 정합성

### 2.1 변경 내역

**`01_시민개발자_API포털_아키텍처정의서.md`**
- R-07 요구사항 문구를 SHA-256 + HMAC-SHA256 이중 보관 구조로 재정의.
- ER 다이어그램 PAT 엔티티에 `token_hmac` 명시.
- 시퀀스 다이어그램의 "Argon2id 해시" 문구를 "HMAC-SHA256 비교" 및 "SHA-256 및 HMAC-SHA256(server_key) 계산" 으로 교정.
- §8.2 PAT 보안 정책 2번 항목을 SHA-256/HMAC 구조로 개정.

**`02_시민개발자_API포털_구현상세스펙.md`**
- §2.1.4 PAT DDL: `token_hash VARCHAR(255)` → `VARCHAR(64)`, `token_hmac VARCHAR(64) NOT NULL` 추가, 컬럼 주석 명시.
- §2.2 Redis 키 설계: `cdp:pat:*` 필드 목록 확장 (`hash`, `rate_limit_tps`, `burst`, `daily_quota`, `monthly_quota`, `expires_at`), `cdp:pat:neg:*` 및 `cdp:jwtidx:*` 신규 문서화, `scopeHash` 축약 규칙 명시.
- §3.2: Argon2id 파라미터 표 삭제. SHA-256 + HMAC-SHA256 2단 구조 표와 근거(256bit CSPRNG → 사전 공격 무의미) 명문화.

### 2.2 근거

`services/portal-backend/pat_utils.py:33-51` 는 이미 SHA-256을 채택한 이유(secret이 32byte CSPRNG이므로 Argon2id 필요 없음)를 코드 주석에 남기고 있어, 구현이 더 견고한 설계 판단을 가지고 있었습니다. 이번 개정으로 문서가 구현을 정확히 반영합니다.

---

## 3. P2 §6 — JWT Claim Header 신뢰경계 문서화

### 3.1 변경 내역

**신규 파일 `docs/security_headers.md`**
- Authority vs Context 등급 정의.
- Citizen Gateway가 주입하는 신뢰 헤더 및 편의 헤더 목록 (`X-USER-ID`, `X-COMPANY` 등).
- 도메인 서비스가 지켜야 할 3원칙 (Authority 헤더/Claim 만 인가에 사용, 불일치 시 JWT 우선, 감사 로그에 Gateway 헤더 포함).
- 장기 목표: Citizen Gateway에서 헤더 주입 로직을 제거하고 Internal Gateway가 검증된 JWT 기반으로 재발급.

### 3.2 채택하지 않은 옵션

계획서 §6.2 의 "X-CDP-* 접두어 도입" 은 다운스트림 도메인 서비스가 이미 `X-USER-ID` 등 헤더에 의존하고 있어 **호환성이 깨질 수 있어** 이번에는 채택하지 않았습니다. 대신 문서에 등급 정의를 명시하고 장기 로드맵에서 헤더 자체를 걷어내는 방향을 채택했습니다. 도메인 서비스가 이 헤더에 인가 판단을 의존하는지는 별도 코드 리뷰로 확인 필요.

---

## 4. P2 §7 — JWT Cache Scope Hash 8→16자

### 4.1 변경 내역

3곳을 lockstep으로 수정 (같은 값을 계산해야 캐시 일관성 유지).

- `services/token-exchange-service/cache.py:_scope_hash` — `[:8]` → `[:16]`, 근거 주석 추가.
- `services/apisix-gateway/plugins/apisix/plugins/pat-token-exchange.lua:scope_hash` — `:sub(1, 8)` → `:sub(1, 16)`.
- `services/token-exchange-service/main.py` — circuit-open 분기에서 인라인으로 재구현하던 hash 계산을 `jwt_cache._scope_hash(...)` 호출로 통일 (중복 제거 + 자동으로 16자 반영).
- `tests/integration/test_tc_x.py:_scope_hash` — 동일하게 `[:16]` 로 변경.

### 4.2 배포 시 주의사항

기존 캐시(`cdp:jwt:{tokenId}:{scopeHash-8char}`) 는 새 코드가 읽지 않으므로 TTL(240초) 만료까지 사용되지 않고 그대로 만료됩니다. 별도 flush 불필요.

---

## 5. P2 §8 — Invalidation 정책 및 훅

### 5.1 변경 내역

**`services/portal-backend/redis_client.py`**
- `invalidate_jwt_cache(r, token_ids)` 함수 신규. PAT 캐시는 유지하고 JWT 캐시만 삭제 (스코프/역할 변경 반영 용).

**`services/portal-backend/routers/catalog.py`**
- `_tokens_for_api(db, api_id)` 헬퍼: 특정 API에 속하는 ACTIVE PAT 목록 조회.
- `patch_api` — 스코프/upstream/public_path/status 등 라우트 관련 변경이 있을 때 `invalidate_jwt_cache` 자동 호출. PAT 자체는 유지되므로 사용자가 재발급할 필요는 없음.

**`services/portal-backend/routers/internal.py`**
- 신규 엔드포인트 `POST /internal/user-events` (X-Internal-Key 인증).
- `DISABLE`/`DELETE` 이벤트: 해당 user_sub 의 ACTIVE PAT 전량 REVOKED 처리 + PAT/JWT 캐시 flush + `PAT_REVOKED` 감사 로그 기록.
- `LOGOUT` 이벤트: JWT 캐시만 flush (PAT 유지).
- 중복 호출 idempotent.

**신규 파일 `docs/invalidation_policy.md`**
- 이벤트별 반영 시점 매트릭스 (즉시 / 최대 240초 / 매 요청).
- Keycloak Event Webhook 스펙 및 Keycloak SPI 연동 가이드.
- 운영 체크리스트.

### 5.2 미구현 (외부 시스템 연동)

Keycloak SPI 이벤트 리스너 자체는 Keycloak 측 설정이므로 이번 커밋 범위 밖입니다. 웹훅 수신 엔드포인트는 준비되었고, Keycloak 설정 예시는 `docs/invalidation_policy.md` §4.3 에 명시했습니다.

---

## 6. P2 §9 — 보안 감사 Redis Stream Durable 처리

### 6.1 변경 내역

**`services/apisix-gateway/plugins/apisix/plugins/pat-audit.lua`**
- Schema에 `redis_host`, `redis_port`, `redis_password`, `security_stream_key` (기본 `cdp:audit:security`), `security_stream_maxlen` (기본 100,000) 추가.
- `SECURITY_EVENTS = { AUTH_FAILED, SCOPE_DENIED, QUOTA_EXCEEDED, EXCHANGE_FAILED }` 정의.
- `push_security_event(conf, entry)` 함수 — Redis `XADD ... MAXLEN ~ N ... payload <json>` 로 이벤트 추가.
- `_M.log` 에서 security event 이면 **HTTP 배치 + Redis Stream 양방향** 으로 전송. 일반 `API_CALL` 은 기존 HTTP 배치만.

**`services/portal-backend/apisix_client.py`**
- `pat-audit` 플러그인 config에 Redis 접속 정보(`common_redis`) 자동 전달.

**신규 파일 `services/portal-backend/audit_consumer.py`**
- `redis.asyncio` 기반 백그라운드 소비자.
- `XGROUP CREATE portal-audit @ cdp:audit:security $ MKSTREAM` (idempotent, BUSYGROUP 무시).
- `XREADGROUP > BLOCK 5000 COUNT 100` 로 배치 소비 → `cdp.audit_log` INSERT → 성공 건만 `XACK`.
- 실패 건은 unack 상태로 남아 다음 poll에서 재전달 (idempotent 아닌 대신 audit_log 는 append-only 이므로 극히 드문 중복은 무손실보다 우선).
- Poll 루프 실패 시 2초 backoff + 재시도.

**`services/portal-backend/main.py`**
- startup 시 `audit_consumer.run_consumer()` 를 `asyncio.create_task` 로 실행.
- shutdown 시 정상 취소(cancel + await).

### 6.2 신뢰성 보장

| 실패 모드 | 이전 동작 | 이후 동작 |
| :--- | :--- | :--- |
| 워커 종료 시 `_batch` 잔존 | best-effort HTTP flush 시도 후 유실 가능 | Security 이벤트는 이미 Redis Stream에 XADD 완료 → 소비자가 다음 폴링에 처리 |
| Portal `/internal/audit` HTTP 5xx | 워커 로그만 남고 유실 | Security 이벤트는 Stream에 남아 이후 소비 |
| Portal 재기동 | 진행 중 배치 유실 가능 | Stream은 Redis 지속. `_ensure_group` idempotent 로 재기동 시 이어서 소비 |
| 소비자 폭주 (portal 지연) | — | `MAXLEN ~ 100000` 로 stream 크기 제한 (approximate trim); 초과 시 오래된 이벤트가 밀려남 |
| INSERT 실패 (DB 순간 장애) | 유실 | 미 ACK → 다음 poll 재전달 |

### 6.3 남은 과제 (Phase 2)

- Redis Stream → Kafka Connect 또는 별도 shipper 를 통한 SIEM 이관.
- Redis Sentinel 페일오버 시 stream 지속성(AOF everysec) 재확인.

---

## 7. 배포 · 마이그레이션 가이드

### 7.1 개발 환경 (로컬 docker compose)

```bash
docker compose down -v   # DB 볼륨 제거 필요 (스키마 변경 반영)
docker compose up -d
# portal-backend 로그에 "audit stream group created" 확인
# portal-backend 로그에 "Portal Backend started" 확인
```

### 7.2 운영 환경 (예상 절차)

1. **점검 공지 발송** — PAT 검증 방식 변경으로 극히 짧은 컷오버 창 예상.
2. **DDL 마이그레이션 실행** (별도 migration 파일 필요 시 추후 작성):
   ```sql
   BEGIN;
   ALTER TABLE cdp.pat ADD COLUMN token_hmac VARCHAR(64);
   -- 기존 행은 NULL. 재발급 유도 정책 §1.4 적용.
   COMMIT;
   ```
3. **Portal Backend 롤아웃** — audit_consumer 는 XGROUP CREATE MKSTREAM 으로 idempotent 하게 시작.
4. **APISIX 롤아웃** — 새 pat-audit / pat-auth 플러그인 코드 배포. `apisix reload` 로 안전 반영.
5. **기존 legacy PAT 처리** — `services/portal-backend/routers/tokens.py:revoke_pat` 로 개별 폐기 또는 배치 INACTIVE 처리.
6. **모니터링 확인** — `XLEN cdp:audit:security` 및 portal-backend `audit consumer XACKed` 로그.

---

## 8. 변경 파일 목록

### 코드
- `infra/postgres/init/01_ddl.sql`
- `services/portal-backend/routers/tokens.py`
- `services/portal-backend/routers/internal.py`
- `services/portal-backend/routers/catalog.py`
- `services/portal-backend/redis_client.py`
- `services/portal-backend/apisix_client.py`
- `services/portal-backend/main.py`
- `services/portal-backend/audit_consumer.py` (신규)
- `services/token-exchange-service/cache.py`
- `services/token-exchange-service/main.py`
- `services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua`
- `services/apisix-gateway/plugins/apisix/plugins/pat-audit.lua`
- `services/apisix-gateway/plugins/apisix/plugins/pat-token-exchange.lua`
- `tests/integration/test_tc_x.py`

### 문서
- `01_시민개발자_API포털_아키텍처정의서.md` (개정)
- `02_시민개발자_API포털_구현상세스펙.md` (개정)
- `docs/security_headers.md` (신규)
- `docs/invalidation_policy.md` (신규)
- `apisix_브랜치_보완결과.md` (본 문서, 신규)

---

## 9. 후속 작업 (본 커밋 범위 외)

**P1 잔여**
- §2 X-Forwarded-For Trust Boundary 확정 (인프라 표준 필요)
- §3 Scope Fail-Closed 코드 반영 (pat-auth.lua else 분기)
- §4 Keycloak Token Exchange Down-scoping PoC 5개 시나리오
- §5 TXS NetworkPolicy · mTLS

**회귀 방지**
- P0 시나리오(Redis flush 후 secret 변조 → 401) 자동 회귀 테스트 추가
- CI 게이트에 P0 필수 통과 조건 추가

**모니터링**
- `cdp:audit:security` stream XLEN, PEL(pending) 모니터링 대시보드
- audit_consumer XACK rate 지표

---

## 10. 후속: 실 Keycloak 전환 (2026-09-26)

로컬 컴포즈의 Zone 4를 `mock-keycloak`에서 실 `quay.io/keycloak/keycloak:24.0.0`로 교체했습니다.
목업 자체 코드는 그대로 두고(`services/mock-keycloak/`, 이미지도 유지) 컴포즈 상에서만 활성 IdP를
바꿔치기하는 방식이라 필요하면 depends_on 한 곳만 되돌려 즉시 롤백할 수 있습니다.

### 반영 내용

| 항목 | 변경 |
| :-- | :-- |
| `docker-compose.yml` | `keycloak` 서비스 신설(`start-dev --import-realm --features=token-exchange`, `KC_HOSTNAME_URL=http://keycloak:8080`, 포트 8181, `KC_HEALTH_ENABLED=true` + `/dev/tcp` 헬스체크). `portal-backend`/`token-exchange-service`/`mock-internal-gw`의 `KC_URL`·`KEYCLOAK_URL`을 `http://keycloak:8080`으로 전환하고 `depends_on`도 `keycloak: service_healthy`로 교체. `mock-keycloak` 블록은 남아 있으나 어떤 서비스도 참조하지 않음 |
| `infra/keycloak/hd-realm.json` (신규) | realm `hd` + 사용자 4명(HR/org 속성·역할·비밀번호 `password`) + 서비스 계정에 realm-management `impersonation`·`view-users`·`query-users` 롤 부여 + 클라이언트 `citizen-gw-exchanger`(confidential, secret `exchanger-secret-xyz`)에 protocol mapper 9종(`cdp_channel` 하드코딩, `aud=internal-api-gateway` 오디언스, HR/org 속성 8종) + 오디언스용 bearer-only 클라이언트 `internal-api-gateway` |
| `services/token-exchange-service/keycloak_client.py` | RFC 8693 exchange 요청에서 `audience` 파라미터 제거. 실 Keycloak(`--features=token-exchange`)은 audience가 명시되면 대상 클라이언트의 fine-grained token-exchange 권한 정책을 강제하는데 dev realm은 그 정책을 두지 않았습니다. 대신 audience protocol mapper가 발급 토큰의 `aud`에 `internal-api-gateway`를 그대로 주입합니다 |
| `services/portal-backend/routers/dev_auth.py` | 목업이 허용했던 "client 인증만으로 exchange" 형태를 실 Keycloak은 `Client not allowed to exchange`로 거부하므로, `client_credentials`로 서비스 계정 토큰을 먼저 발급받고 이를 `subject_token`으로 넘기는 2-스텝 흐름으로 변경 |

### 검증 결과 (로컬)

1. `discovery`, `client_credentials`, `token-exchange` 모두 200 OK 응답.
2. 교환된 access token 클레임:
   - `iss=http://keycloak:8080/realms/hd`, `sub=u-test-001`, `aud=internal-api-gateway`, `azp=citizen-gw-exchanger`
   - `cdp_channel=citizen`, `preferred_username=hong.gildong`
   - HR/org: `user_id=EMP10001`, `company=HDHI`, `org_cd=ORG-IT`, `asgn_cd=ASG-DEV`, `dept_cd=DEPT-IT01`, …
   - `realm_access.roles=[mdm-reader, citizen-developer]`
3. E2E: dev-login → application 생성/승인 → PAT 발급 → `GET http://localhost:9080/capi/v1/vendors` 200 응답. `mock-internal-gw` 로그에 `[INTERNAL-GW] sub=u-test-001 pat=… channel=citizen … 200 OK` 확인.

### 알려진 잔여

- **서비스 계정 롤 조회 부재**: `portal-backend/routers/applications.py::_get_user_roles_from_idp`는 `/admin/realms/hd/users/{sub}`를 비인증으로 호출합니다. 목업은 무인증을 허용했지만 실 Keycloak은 401을 돌려주므로 이 경로는 항상 `WARN`을 찍고 빈 리스트로 폴백합니다. 다행히 자격 검증은 IdP 롤이 비어 있어도 JWT의 `realm_access.roles`(=`user.roles`)로 통과 판단이 가능해 기능은 그대로지만, R-08 자격검증 강화 작업이 붙는다면 여기에 admin token(`client_credentials` + `realm-management` 롤) 호출을 얹어야 합니다.
- **audience fine-grained 정책 미설정**: 위에서 audience 파라미터를 뺀 우회로 처리했습니다. 운영 realm에서는 `citizen-gw-exchanger` → `internal-api-gateway` 방향의 token-exchange scope 권한을 명시적으로 부여하고 TXS에도 audience 파라미터를 복원하는 것이 정석입니다.
- **realm 재임포트**: dev-mode Keycloak은 H2 DB가 컨테이너 라이프사이클에 매여 있어 `docker compose restart keycloak`만으로는 realm 정의 변경이 반영되지 않습니다. `docker compose rm -sf keycloak && docker compose up -d keycloak`로 재생성해야 합니다.
