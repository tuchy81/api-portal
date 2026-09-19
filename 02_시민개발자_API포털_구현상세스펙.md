# 시민개발자 API 포털 구현 상세 스펙

**문서 ID**: SA-SPEC-CDP-002
**버전**: v1.0
**상위 문서**: SA-ARCH-CDP-001 『시민개발자 API 포털 아키텍처 정의서』
**작성**: 시스템아키텍처팀 (DX)
**작성일**: 2026-09-18

---

## 1. 구현 범위 및 산출물

| 모듈 | 저장소(예시) | 스택 | 주요 산출물 |
| :--- | :--- | :--- | :--- |
| 포털 백엔드 | `citizen-portal-api` | **Spring Boot 3.3 / Java 21** | PAT·신청·카탈로그 REST API, BFF 세션, 배치 |
| 포털 프론트 | `citizen-portal-web` | **Vue 3.5 / Vite 6 / TypeScript** | MFE Remote 모듈, 카탈로그·신청·PAT·대시보드 화면 10종 |
| 토큰 교환 | `citizen-token-exchange` | **Spring Boot 3.3 / Java 21** | RFC 8693 클라이언트, JWT 캐시, 서킷브레이커 |
| Gateway 플러그인 | `citizen-apisix-plugin` | APISIX 3.9 / Lua | `pat-auth`, `pat-quota`, `pat-audit` |
| 인프라 | `citizen-infra` | Helm / Terraform | K8s 매니페스트, APISIX·Redis·PG 구성 |

---

## 2. 데이터베이스 스펙 (PostgreSQL 16)

### 2.1 DDL

```sql
CREATE SCHEMA IF NOT EXISTS cdp;

-- 2.1.1 공개 API 카탈로그
CREATE TABLE cdp.api_catalog (
    api_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    api_code        VARCHAR(64)  NOT NULL UNIQUE,      -- 예: MDM-VENDOR
    name            VARCHAR(200) NOT NULL,
    description     TEXT,
    owner_dept      VARCHAR(100) NOT NULL,
    owner_sub       VARCHAR(64)  NOT NULL,             -- Keycloak sub
    upstream_url    VARCHAR(500) NOT NULL,             -- 내부 GW 주소
    public_path     VARCHAR(200) NOT NULL UNIQUE,      -- 예: /capi/v1/vendors
    openapi_spec    JSONB,
    required_roles  TEXT[]       NOT NULL DEFAULT '{}',-- 신청 자격 Role
    status          VARCHAR(20)  NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT','PUBLISHED','DEPRECATED','RETIRED')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 2.1.2 API Scope 정의
CREATE TABLE cdp.api_scope (
    scope_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    api_id       UUID        NOT NULL REFERENCES cdp.api_catalog(api_id) ON DELETE CASCADE,
    scope_name   VARCHAR(100) NOT NULL,                -- 예: capi.vendor.read
    http_method  VARCHAR(10)  NOT NULL,                -- GET/POST/PUT/DELETE/*
    path_pattern VARCHAR(300) NOT NULL,                -- 예: /capi/v1/vendors/**
    description  VARCHAR(300),
    UNIQUE (api_id, scope_name, http_method, path_pattern)
);

-- 2.1.3 사용 신청
CREATE TABLE cdp.application (
    app_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    api_id         UUID        NOT NULL REFERENCES cdp.api_catalog(api_id),
    user_sub       VARCHAR(64) NOT NULL,
    user_name      VARCHAR(100) NOT NULL,
    dept_code      VARCHAR(50),
    purpose        TEXT        NOT NULL,
    expected_tps   INT         NOT NULL DEFAULT 1,
    expected_daily INT         NOT NULL DEFAULT 1000,
    valid_until    DATE        NOT NULL,
    status         VARCHAR(30) NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED','WITHDRAWN')),
    granted_scopes TEXT[]      NOT NULL DEFAULT '{}',
    reviewer_sub   VARCHAR(64),
    review_comment TEXT,
    reviewed_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_app_user   ON cdp.application(user_sub, status);
CREATE INDEX idx_app_status ON cdp.application(status, created_at DESC);

-- 2.1.4 PAT
CREATE TABLE cdp.pat (
    token_id     VARCHAR(12)  PRIMARY KEY,             -- 공개 식별자
    app_id       UUID         NOT NULL REFERENCES cdp.application(app_id),
    user_sub     VARCHAR(64)  NOT NULL,
    token_name   VARCHAR(100) NOT NULL,
    token_hash   VARCHAR(255) NOT NULL,                -- Argon2id
    scopes       TEXT[]       NOT NULL,
    status       VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE'
                 CHECK (status IN ('ACTIVE','REVOKED','EXPIRED','INACTIVE')),
    allowed_cidr TEXT[]       DEFAULT '{}',            -- 선택적 IP 제한
    issued_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ  NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    revoked_by   VARCHAR(64),
    revoke_reason VARCHAR(300)
);
CREATE INDEX idx_pat_user    ON cdp.pat(user_sub, status);
CREATE INDEX idx_pat_expires ON cdp.pat(expires_at) WHERE status = 'ACTIVE';

-- 2.1.5 쿼터 정책
CREATE TABLE cdp.quota_policy (
    policy_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    token_id       VARCHAR(12) NOT NULL UNIQUE REFERENCES cdp.pat(token_id) ON DELETE CASCADE,
    rate_limit_tps INT         NOT NULL DEFAULT 10,
    burst          INT         NOT NULL DEFAULT 20,
    daily_quota    INT         NOT NULL DEFAULT 5000,
    monthly_quota  INT         NOT NULL DEFAULT 100000,
    concurrency    INT         NOT NULL DEFAULT 5,
    updated_by     VARCHAR(64),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2.1.6 감사 로그 (월 파티션)
CREATE TABLE cdp.audit_log (
    log_id       BIGSERIAL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type   VARCHAR(40) NOT NULL,                 -- API_CALL/AUTH_FAILED/...
    token_id     VARCHAR(12),
    user_sub     VARCHAR(64),
    jwt_jti      VARCHAR(64),
    trace_id     VARCHAR(64),
    client_ip    INET,
    http_method  VARCHAR(10),
    request_path VARCHAR(500),
    status_code  INT,
    latency_ms   INT,
    error_code   VARCHAR(20),
    detail       JSONB,
    PRIMARY KEY (log_id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE cdp.audit_log_2026_10 PARTITION OF cdp.audit_log
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

-- 2.1.7 사용량 집계 (일 단위)
CREATE TABLE cdp.usage_stat_daily (
    stat_date    DATE        NOT NULL,
    token_id     VARCHAR(12) NOT NULL,
    api_id       UUID        NOT NULL,
    call_count   BIGINT      NOT NULL DEFAULT 0,
    error_count  BIGINT      NOT NULL DEFAULT 0,
    avg_latency  INT         NOT NULL DEFAULT 0,
    p95_latency  INT         NOT NULL DEFAULT 0,
    PRIMARY KEY (stat_date, token_id, api_id)
);
```

### 2.2 Redis 키 설계

| 키 패턴 | 타입 | TTL | 값 | 용도 |
| :--- | :--- | :--- | :--- | :--- |
| `cdp:pat:{tokenId}` | Hash | PAT 만료시각까지 | `hash`, `sub`, `scopes`, `status`, `cidr` | Gateway PAT 검증 |
| `cdp:jwt:{tokenId}:{scopeHash}` | String | 240s | JWT 문자열 | 교환 결과 캐시 |
| `cdp:quota:d:{tokenId}:{yyyyMMdd}` | String(INCR) | 익일 00:05 | 누적 호출 수 | 일일 쿼터 |
| `cdp:quota:m:{tokenId}:{yyyyMM}` | String(INCR) | 익월 5일 | 누적 호출 수 | 월 쿼터 |
| `cdp:rl:{tokenId}` | Hash | 60s | 토큰버킷 `tokens`, `ts` | TPS 제한 |
| `cdp:cc:{tokenId}` | String(INCR/DECR) | 30s | 동시 처리 수 | 동시성 제한 |
| `cdp:cb:txs` | String | 30s | `OPEN`/`HALF_OPEN` | 서킷브레이커 상태 |

### 2.3 쿼터 차감 Lua 스크립트 (원자성 보장)

```lua
-- KEYS[1]=rate limit key, KEYS[2]=daily key, KEYS[3]=monthly key
-- ARGV[1]=tps, ARGV[2]=burst, ARGV[3]=now(ms),
-- ARGV[4]=dailyQuota, ARGV[5]=monthlyQuota,
-- ARGV[6]=dailyTtl, ARGV[7]=monthlyTtl
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local ts     = tonumber(redis.call('HGET', KEYS[1], 'ts'))
local tps    = tonumber(ARGV[1])
local burst  = tonumber(ARGV[2])
local now    = tonumber(ARGV[3])

if tokens == nil then tokens = burst; ts = now end
local refill = (now - ts) / 1000 * tps
tokens = math.min(burst, tokens + refill)

if tokens < 1 then
  redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
  redis.call('EXPIRE', KEYS[1], 60)
  return {0, 'RATE_LIMIT', 0}          -- 429
end

local d = tonumber(redis.call('GET', KEYS[2]) or '0')
if d >= tonumber(ARGV[4]) then return {0, 'DAILY_QUOTA', 0} end
local m = tonumber(redis.call('GET', KEYS[3]) or '0')
if m >= tonumber(ARGV[5]) then return {0, 'MONTHLY_QUOTA', 0} end

tokens = tokens - 1
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], 60)
d = redis.call('INCR', KEYS[2]); redis.call('EXPIRE', KEYS[2], tonumber(ARGV[6]))
m = redis.call('INCR', KEYS[3]); redis.call('EXPIRE', KEYS[3], tonumber(ARGV[7]))

return {1, 'OK', tonumber(ARGV[4]) - d}  -- 허용, 잔여 일일 쿼터
```

---

## 3. PAT 사양

### 3.1 토큰 포맷
```
hdpat_<tokenId:12>_<secret:43>
예) hdpat_a7Kd93mQx2Lp_Xk8sT2vQmN4rZ...   (총 62자)
```
- `tokenId`: base62 12자 (공개 식별자, DB PK, 로그 기록 대상)
- `secret`: 256bit CSPRNG → base64url 43자 (무패딩)
- 접두사 `hdpat_`: 시크릿 스캐너 탐지 패턴 `hdpat_[A-Za-z0-9]{12}_[A-Za-z0-9_-]{43}`

### 3.2 해시 파라미터 (Argon2id)
| 파라미터 | 값 | 비고 |
| :--- | :--- | :--- |
| memory | 19 MiB (19456 KB) | OWASP 권고 최소 구성 |
| iterations | 2 | |
| parallelism | 1 | Gateway 검증 지연 고려 |
| salt | 16 bytes (랜덤) | |
| output | 32 bytes | |

> **Gateway 성능 고려**: Argon2id 검증은 요청마다 수행 시 지연이 크므로, **Redis에 `tokenId → HMAC-SHA256(secret, serverKey)` 사전계산 값**을 함께 저장하여 Gateway 단에서는 상수시간 HMAC 비교로 처리. Argon2id 해시는 DB 원장 및 캐시 미스 시 포털 백엔드 검증용으로만 사용.

### 3.3 상태 전이

```
ACTIVE ──(관리자/본인 폐기)──▶ REVOKED   (복구 불가)
ACTIVE ──(expires_at 경과)───▶ EXPIRED   (복구 불가, 재발급 필요)
ACTIVE ──(30일 미사용 배치)──▶ INACTIVE  (관리자 승인 시 ACTIVE 복귀 가능)
```

---

## 4. 포털 백엔드 API 스펙

- Base URL: `https://cdp-portal.hd.com/portal/v1`
- 인증: 사내 SSO(OIDC Authorization Code + PKCE) 발급 Access Token, `Authorization: Bearer <JWT>`
- 오류 포맷: RFC 9457 `application/problem+json`

### 4.1 엔드포인트 목록

| Method | Path | 설명 | 권한 |
| :--- | :--- | :--- | :--- |
| GET | `/catalog/apis` | 공개 API 목록(검색·페이징) | 로그인 사용자 |
| GET | `/catalog/apis/{apiId}` | API 상세 + OpenAPI 스펙 | 로그인 사용자 |
| POST | `/catalog/apis` | API 공개 등록 | API 오너, 관리자 |
| PATCH | `/catalog/apis/{apiId}` | 카탈로그 수정·상태 변경 | API 오너, 관리자 |
| POST | `/applications` | 사용 신청 | 로그인 사용자 |
| GET | `/applications` | 신청 목록(본인/결재 대상) | 로그인 사용자 |
| PATCH | `/applications/{appId}` | 승인/반려 | API 오너, 관리자 |
| POST | `/tokens` | PAT 발급 | 승인 신청 보유자 |
| GET | `/tokens` | 본인 PAT 목록(메타만) | 로그인 사용자 |
| DELETE | `/tokens/{tokenId}` | PAT 폐기 | 소유자, 관리자 |
| PUT | `/tokens/{tokenId}/quota` | 쿼터 정책 변경 | 관리자 |
| GET | `/usage/tokens/{tokenId}` | 사용량 통계 | 소유자, 관리자 |
| GET | `/audit/logs` | 감사 로그 조회 | 감사자, 관리자 |

### 4.2 주요 API 상세

#### 4.2.1 PAT 발급 — `POST /portal/v1/tokens`

요청
```json
{
  "appId": "3f8c1a52-9d1b-4f77-bb2c-6a1d0e9f4c11",
  "tokenName": "월별 수주현황 집계 스크립트",
  "validDays": 90,
  "allowedCidr": ["10.20.0.0/16"]
}
```

응답 `201 Created`
```json
{
  "tokenId": "a7Kd93mQx2Lp",
  "token": "hdpat_a7Kd93mQx2Lp_Xk8sT2vQmN4rZp1LwEu6Yb3Hc9Ad5Jf0Kg2",
  "tokenName": "월별 수주현황 집계 스크립트",
  "scopes": ["capi.vendor.read", "capi.order.read"],
  "quota": { "rateLimitTps": 10, "dailyQuota": 5000, "monthlyQuota": 100000 },
  "issuedAt": "2026-09-18T09:12:33+09:00",
  "expiresAt": "2026-12-17T09:12:33+09:00",
  "warning": "토큰 평문은 본 응답에서만 확인 가능합니다. 재조회할 수 없습니다."
}
```

오류
| HTTP | code | 상황 |
| :--- | :--- | :--- |
| 400 | `CDP-4001` | validDays 범위(1~90) 초과 |
| 403 | `CDP-4003` | 신청 건 미승인 또는 소유자 불일치 |
| 409 | `CDP-4009` | 활성 PAT 개수 상한 초과 |

#### 4.2.2 사용 신청 — `POST /portal/v1/applications`

요청
```json
{
  "apiId": "b91f...",
  "purpose": "협력사 현황 주간 리포트 자동 생성",
  "requestedScopes": ["capi.vendor.read"],
  "expectedTps": 5,
  "expectedDaily": 2000,
  "validUntil": "2027-03-31"
}
```

응답 `201 Created`
```json
{
  "appId": "3f8c1a52-...",
  "status": "PENDING",
  "eligibility": { "checked": true, "missingRoles": [] },
  "reviewers": [{ "sub": "u-10233", "name": "김OO", "dept": "MDM운영팀" }]
}
```

#### 4.2.3 쿼터 변경 — `PUT /portal/v1/tokens/{tokenId}/quota`
```json
{ "rateLimitTps": 20, "burst": 40, "dailyQuota": 20000, "monthlyQuota": 400000, "concurrency": 10 }
```
- 처리: DB 갱신 → Redis `cdp:pat:{tokenId}` 갱신 → 즉시 반영(재기동 불요)

#### 4.2.4 오류 응답 표준 (RFC 9457)
```json
{
  "type": "https://cdp-portal.hd.com/errors/CDP-1004",
  "title": "Quota exceeded",
  "status": 429,
  "code": "CDP-1004",
  "detail": "일일 호출 한도 5,000건을 초과했습니다.",
  "instance": "/capi/v1/vendors",
  "traceId": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

### 4.3 오류 코드 체계

| 코드 | HTTP | 의미 | 발생 지점 |
| :--- | :--- | :--- | :--- |
| CDP-1001 | 401 | PAT 무효/폐기/만료 | Gateway |
| CDP-1002 | 401 | PAT 형식 오류 | Gateway |
| CDP-1003 | 403 | Scope 불일치 | Gateway |
| CDP-1004 | 429 | Rate Limit / Quota 초과 | Gateway |
| CDP-1005 | 403 | 내부 PDP 인가 거부 | 내부 GW |
| CDP-1006 | 403 | 허용 IP(CIDR) 위반 | Gateway |
| CDP-2001 | 503 | 토큰 교환 실패 / IdP 장애 | TXS |
| CDP-2002 | 502 | Upstream 오류 | Gateway |
| CDP-4001 | 400 | 요청 값 검증 실패 | Portal |
| CDP-4003 | 403 | 포털 권한 없음 | Portal |
| CDP-4009 | 409 | 리소스 상태 충돌 | Portal |

---

## 5. Token Exchange Service 스펙

### 5.1 내부 엔드포인트 — `POST /internal/token-exchange`
- 접근 제한: Gateway Pod IP 또는 mTLS 클라이언트 인증서

요청
```json
{ "tokenId": "a7Kd93mQx2Lp", "userSub": "u-10233", "scopes": ["capi.vendor.read"] }
```
응답 `200 OK`
```json
{ "accessToken": "eyJhbGciOiJSUzI1...", "expiresIn": 300, "jti": "8f2a...", "cached": false }
```

### 5.2 Keycloak Token Exchange 호출 사양

```http
POST /realms/hd/protocol/openid-connect/token HTTP/1.1
Host: sso.hd.com
Content-Type: application/x-www-form-urlencoded
Authorization: Basic base64(citizen-gw-exchanger:<client-secret>)

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&subject_token=<TXS 서비스 계정 Access Token>
&subject_token_type=urn:ietf:params:oauth:token-type:access_token
&requested_subject=u-10233
&requested_token_type=urn:ietf:params:oauth:token-type:access_token
&audience=internal-api-gateway
&scope=capi.vendor.read
```

### 5.3 Keycloak 구성 요건
| 항목 | 설정 |
| :--- | :--- |
| Realm | `hd` (기존 사내 Realm 재사용) |
| Exchanger Client | `citizen-gw-exchanger` — Confidential, Service Account 활성, Standard Flow 비활성 |
| Target Client(Audience) | `internal-api-gateway` |
| 기능 플래그 | `--features=token-exchange,admin-fine-grained-authz` |
| 권한 부여 | Exchanger Client에 `impersonation` Role 또는 Token Exchange Permission (target client 대상) 부여 |
| Client Scope | `capi.*` 각 Scope를 Optional Client Scope로 등록, Audience Mapper 추가 |
| 토큰 수명 | Access Token Lifespan 300초 (교환 토큰 전용 Client Override) |
| Custom Claim | `cdp_pat_id`(PAT ID), `cdp_channel="citizen"` — Hardcoded/Script Mapper로 주입 |

> **[중요] 버전 의존성 경고**: 위 요청 형식은 `requested_subject`를 사용하는 **사용자 임퍼소네이션** 방식으로, Keycloak 26.2부터 정식 지원되는 **Standard Token Exchange(V2)에서는 지원되지 않습니다(Not implemented yet)**. 해당 기능은 **Legacy Token Exchange(V1)** 에만 존재하며, V1은 Preview + Deprecated 등급으로 향후 제거가 예고되어 있습니다.
>
> 따라서 기능 플래그 `token-exchange:v1` 및 `admin-fine-grained-authz:v1` 활성화가 전제되며, 운영 적용 전 **부록 C의 판정 절차(C.1)를 반드시 수행**하고 방식 A~D 중 하나를 확정해야 합니다. 구축 절차·검증 기준·트러블슈팅은 **부록 C 『Keycloak Token Exchange 구축 매뉴얼』** 참조.

### 5.4 복원력(Resilience) 설정
```yaml
resilience4j:
  circuitbreaker:
    instances:
      keycloakExchange:
        slidingWindowSize: 50
        failureRateThreshold: 50
        waitDurationInOpenState: 30s
        permittedNumberOfCallsInHalfOpenState: 5
  retry:
    instances:
      keycloakExchange:
        maxAttempts: 2
        waitDuration: 200ms
        retryExceptions: [java.io.IOException, org.springframework.web.client.HttpServerErrorException]
  timelimiter:
    instances:
      keycloakExchange:
        timeoutDuration: 3s
```
- 서킷 OPEN 시: 캐시된 JWT가 있으면 사용(최대 잔여 TTL), 없으면 `CDP-2001` 즉시 반환
- Bulkhead: 동시 교환 요청 최대 100건, 대기열 200

---

### 5.5 JWT 캐시 전략 및 Stampede 대응

**캐시 원칙**
- 교환은 **캐시 미스 시에만** 수행. 매 API 호출마다 Keycloak을 호출하지 않음
- 캐시 키: `cdp:jwt:{tokenId}:{scopeHash}` — Scope 변경 시 키가 자동 분기되어 구 권한 JWT 재사용 차단
- 캐시 TTL 240초 = JWT TTL 300초의 80%. 60초 여유는 in-flight 시간·클럭 스큐·내부 GW `exp` 검증 leeway 대응용
- 10 TPS PAT 기준 실효 교환 빈도: 240초당 1회 (캐시 적중률 99.9% 이상)

**Cache Stampede(Thundering Herd) 방지 — Single Flight**
```java
String key = "cdp:jwt:" + tokenId + ":" + scopeHash;
String jwt = redis.get(key);
if (jwt != null) return jwt;

String lockKey = "cdp:lock:jwt:" + tokenId + ":" + scopeHash;
if (redis.set(lockKey, nodeId, SetParams.setParams().nx().px(5000))) {
    try {
        jwt = keycloakExchange(tokenId, userSub, scopes);   // 실제 교환 1건만 수행
        redis.setex(key, 240, jwt);
        redis.sadd("cdp:jwtidx:" + tokenId, key);           // 폐기용 색인
        redis.expire("cdp:jwtidx:" + tokenId, 300);
        return jwt;
    } finally {
        redis.eval(UNLOCK_IF_MINE, 1, lockKey, nodeId);     // 소유자 확인 후 해제
    }
}
// 락 획득 실패 → 50ms 간격 최대 6회 재조회 (총 300ms)
for (int i = 0; i < 6; i++) {
    Thread.sleep(50);
    jwt = redis.get(key);
    if (jwt != null) return jwt;
}
throw new TokenExchangeTimeoutException("CDP-2001");
```

**확률적 조기 갱신(Probabilistic Early Expiration)**
- 잔여 TTL이 전체의 20% 미만(48초 이하)인 구간에서, 확률 `p = (48 - 잔여TTL) / 48` 로 선제 재발급 수행
- 만료 순간 동시 미스가 아니라 시간축으로 분산되어 Keycloak 부하 스파이크 제거

**폐기 시 캐시 정리 — 와일드카드 DEL 금지**
- `DEL cdp:jwt:{tokenId}:*` 는 `KEYS`/`SCAN`을 유발하므로 운영 환경에서 사용 금지
- 발급된 JWT 캐시 키를 `cdp:jwtidx:{tokenId}` Set에 색인해두고, 폐기 시 Set 순회 삭제
```
SMEMBERS cdp:jwtidx:{tokenId} → 각 키 DEL → DEL cdp:jwtidx:{tokenId}
```
- PAT 메타(`cdp:pat:{tokenId}`) 삭제가 선행 검증 단계이므로, JWT 캐시가 잔존해도 호출은 즉시 401 차단됨 (이중 안전장치)

---

## 6. APISIX Gateway 구현 스펙

### 6.1 라우트 정의 예시

```yaml
routes:
  - id: capi-vendor-v1
    uri: /capi/v1/vendors/*
    methods: [GET, POST]
    upstream_id: internal-gateway
    plugins:
      pat-auth:
        redis_cluster: ["redis-0:6379","redis-1:6379","redis-2:6379"]
        required_scope_map:
          "GET:/capi/v1/vendors/**": "capi.vendor.read"
          "POST:/capi/v1/vendors": "capi.vendor.write"
      pat-quota:
        redis_cluster: ["redis-0:6379","redis-1:6379","redis-2:6379"]
        show_limit_headers: true
      proxy-rewrite:
        regex_uri: ["^/capi/v1/(.*)", "/api/v1/$1"]
      pat-audit:
        log_endpoint: "http://citizen-portal-api:8080/internal/audit"
      opentelemetry:
        sampler: { name: always_on }
    priority: 100

upstreams:
  - id: internal-gateway
    type: roundrobin
    scheme: https
    nodes: { "internal-gw.hd.com:443": 1 }
    timeout: { connect: 3, send: 30, read: 30 }
    retries: 1
```

### 6.2 `pat-auth` 플러그인 처리 순서 (phase: `rewrite`)
1. `Authorization` 헤더 파싱 — `Bearer hdpat_...` 미일치 시 `401 CDP-1002`
2. `tokenId` 추출 → Redis `HGETALL cdp:pat:{tokenId}`
   - 미존재 시 포털 백엔드 `GET /internal/pat/{tokenId}` 조회 후 캐시 적재(Negative Cache 30초)
3. `status != ACTIVE` 또는 `expires_at < now` → `401 CDP-1001`
4. `HMAC-SHA256(secret, serverKey)` 상수시간 비교 → 불일치 `401 CDP-1001`
5. `allowed_cidr` 설정 시 `X-Forwarded-For` 검증 → 위반 `403 CDP-1006`
6. 요청 `METHOD:PATH` ↔ `required_scope_map` 매칭 → 부여 Scope 미포함 시 `403 CDP-1003`
7. `ctx.var.cdp_token_id`, `cdp_user_sub`, `cdp_scopes` 설정 후 다음 플러그인 위임

### 6.3 `pat-quota` 플러그인 (phase: `access`)
- 6.2 통과 시 2.3절 Lua 스크립트 `EVALSHA` 실행
- 실패 시 `429 CDP-1004`, 헤더 `Retry-After`, `X-RateLimit-*` 부여
- 성공 시 응답 헤더에 잔여 쿼터 주입
```
X-RateLimit-Limit: 5000
X-RateLimit-Remaining: 4231
X-RateLimit-Reset: 1758200400
X-RateLimit-Policy: 10;w=1, 5000;w=86400, 100000;w=2592000
```

### 6.4 토큰 교환 및 헤더 치환 (phase: `access` 후단)
```
1) Redis GET cdp:jwt:{tokenId}:{scopeHash}
   HIT  → JWT 사용
   MISS → http.request(TXS /internal/token-exchange, timeout=3s)
2) core.request.set_header(ctx, "Authorization", "Bearer " .. jwt)
3) 부가 헤더 주입
   X-Citizen-PAT-Id: {tokenId}
   X-Citizen-Channel: citizen
   X-Request-Id: {trace_id}
4) 클라이언트가 보낸 다음 헤더는 전량 제거(스푸핑 방지)
   X-Citizen-*, X-Forwarded-User, X-User-Sub, Cookie
```

### 6.5 `pat-audit` 플러그인 (phase: `log`)
- 비동기 배치 전송(`batch-processor`, 최대 100건 / 5초)
- 전송 필드: `token_id`, `user_sub`, `jwt_jti`, `trace_id`, `client_ip`, `method`, `path`, `status`, `latency_ms`, `error_code`
- **마스킹 규칙**: PAT 평문은 어떤 경우에도 로그에 기록 금지. 오류 메시지 내 토큰 문자열은 `hdpat_{tokenId}_****`로 치환

---

## 7. 포털 프론트엔드 (Vue 3) 구현 스펙

### 7.1 기술 구성

| 항목 | 채택 | 비고 |
| :--- | :--- | :--- |
| 프레임워크 | Vue 3.5 (Composition API, `<script setup>`) | 팀 표준 |
| 빌드 | Vite 6 + `@originjs/vite-plugin-federation` | 기존 MFE Shell과 동일 구성 |
| 패키지 매니저 | Yarn 4.x (Berry) | 사내 표준 |
| 언어 | TypeScript 5.x (`strict: true`) | API 타입은 OpenAPI 스펙에서 자동 생성 |
| 상태관리 | Pinia | 스토어 4종 (auth, catalog, application, token) |
| 라우팅 | Vue Router — **Shell 통합 시 hash 모드** | 기존 Shell 규약 승계 |
| UI 컴포넌트 | 사내 디자인 시스템 (DxBuilder Nexus 팔레트 적용) | 미구비 항목은 PrimeVue 등으로 보완 |
| 차트 | ECharts (vue-echarts) | 사용량 대시보드 |
| API 문서 뷰어 | Swagger UI (`swagger-ui-dist`) 임베드 | OpenAPI 스펙 렌더링 |
| HTTP | Axios 인스턴스 1개 (`withCredentials: true`) | BFF 세션 쿠키 전송 |

### 7.2 MFE 통합 구조

기존 Vue 3 MFE Module Federation Shell에 **Remote 모듈 1개**로 편입합니다.

```javascript
// vite.config.ts (citizen-portal-web : Remote)
federation({
  name: 'cdpPortal',
  filename: 'remoteEntry.js',
  exposes: {
    './CdpApp': './src/CdpApp.vue',       // 포털 루트 컴포넌트
  },
  shared: {
    vue:          { singleton: true, requiredVersion: '^3.5.0' },
    'vue-router': { singleton: true },
    pinia:        { singleton: true },
  },
})
```

**Shell ↔ Remote 계약**

| 항목 | 규약 |
| :--- | :--- |
| 마운트 | Shell의 탭 추가 → `./CdpApp` 비동기 로드, `Suspense`로 로딩 처리 |
| DOM 보존 | Shell의 `v-show` 기반 탭 전환 규약 승계 (탭 이동 시 상태 유지) |
| 라우팅 | Shell 라우터의 `/cdp` 하위 경로를 Remote가 담당, hash 모드 |
| 인증 | **Remote는 인증을 직접 수행하지 않음** — Shell의 SSO 세션과 단일 BFF URL 승계 |
| 통신 | Shell → Remote 는 props, Remote → Shell 은 `emit` (전역 이벤트 버스 사용 금지) |
| 독립 배포 | 라우팅 경계를 유지해 필요 시 독립 SPA로 분리 배포 가능 |

> **주의**: `shared` 싱글톤 설정을 누락하면 Vue 인스턴스가 이중 로드되어 Pinia 스토어와 `provide/inject`가 분리됩니다. Shell과 Remote의 Vue·Router·Pinia 버전을 동일 메이저로 고정하십시오. (RK-04)

### 7.3 화면 및 라우트 구성

| 라우트 | 화면 | 주요 컴포넌트 | 접근 권한 |
| :--- | :--- | :--- | :--- |
| `/cdp/catalog` | API 카탈로그 목록 | `CatalogList.vue`, `ApiCard.vue`, `CatalogFilter.vue` | 로그인 사용자 |
| `/cdp/catalog/:apiId` | API 상세 · 스펙 | `ApiDetail.vue`, `SwaggerViewer.vue`, `ScopeTable.vue` | 로그인 사용자 |
| `/cdp/apply/:apiId` | 사용 신청 폼 | `ApplicationForm.vue`, `EligibilityBadge.vue` | 로그인 사용자 |
| `/cdp/applications` | 내 신청 현황 | `MyApplications.vue`, `StatusChip.vue` | 로그인 사용자 |
| `/cdp/approvals` | 결재함 | `ApprovalInbox.vue`, `ScopeGrantDialog.vue` | API 오너, 관리자 |
| `/cdp/tokens` | PAT 목록·관리 | `TokenList.vue`, `TokenIssueDialog.vue`, `QuotaBar.vue` | 로그인 사용자 |
| `/cdp/usage` | 사용량 대시보드 | `UsageDashboard.vue`, `CallTrendChart.vue`, `QuotaGauge.vue` | 로그인 사용자 |
| `/cdp/admin/tokens` | 전체 PAT 관리 | `AdminTokenTable.vue`, `QuotaPolicyForm.vue` | 관리자 |
| `/cdp/admin/catalog` | API 공개 등록 | `CatalogRegisterForm.vue`, `OpenApiUploader.vue` | API 오너, 관리자 |
| `/cdp/admin/audit` | 감사 로그 | `AuditLogTable.vue`, `LogDetailDrawer.vue` | 감사자, 관리자 |

**Pinia 스토어**

| 스토어 | 상태 | 비고 |
| :--- | :--- | :--- |
| `useAuthStore` | 사용자 프로필, 롤, 권한 판정 헬퍼 | Shell 주입 또는 `/portal/v1/me` 조회 |
| `useCatalogStore` | API 목록, 검색 조건, 상세 캐시 | 목록 5분 캐시 |
| `useApplicationStore` | 내 신청, 결재 대상 목록 | 폴링 없음, 진입 시 조회 |
| `useTokenStore` | PAT 목록, 쿼터 현황 | **평문 토큰은 스토어에 저장 금지** |

### 7.4 PAT 발급 다이얼로그 — 보안 UX 요건 (필수)

평문 토큰이 노출되는 유일한 지점이므로 아래를 구현 필수 항목으로 규정합니다.

1. 발급 버튼 → 확인 다이얼로그(용도·유효기간·부여 Scope 재확인) 후 요청
2. 성공 시 **평문 토큰 1회 표시** + 복사 버튼 + 경고 배너 "이 창을 닫으면 다시 확인할 수 없습니다"
3. **복사 확인 체크박스 선택 전 닫기·ESC·배경 클릭 비활성화** (`persistent` 모달)
4. `.env` 등 안전한 보관 가이드 링크 제공, 소스코드 하드코딩 금지 문구 명시
5. 토큰 값은 **Pinia 스토어·localStorage·sessionStorage에 저장 금지**, 컴포넌트 로컬 `ref`로만 보유
6. 다이얼로그 `unmounted` 훅에서 `ref.value = null` 로 즉시 소거
7. 복사는 `navigator.clipboard.writeText()` 사용, 실패 시 수동 선택 영역 노출

```vue
<script setup lang="ts">
const plainToken = ref<string | null>(null)   // 스토어 아님 — 로컬 전용
const copied = ref(false)

async function issue(payload: IssueRequest) {
  const { data } = await api.post('/portal/v1/tokens', payload)
  plainToken.value = data.token                // 화면 표시용 1회 보관
  await tokenStore.refreshList()               // 목록은 메타만 갱신
}

onUnmounted(() => { plainToken.value = null }) // 명시적 소거
</script>
```

### 7.5 인증 연동 (BFF 패턴 승계)

- **포털 UI는 OIDC 토큰을 직접 다루지 않습니다.** 기존 BFF 토큰 핸들러 패턴을 그대로 승계
- 인증 흐름: 브라우저 → Shell/포털 백엔드(BFF) → Keycloak Authorization Code + PKCE
- 세션: `HttpOnly` `Secure` `SameSite=Strict` 쿠키. 프론트 JS는 토큰에 접근 불가
- API 호출: Axios `withCredentials: true`, 리버스 프록시 경유 **단일 BFF URL** 사용 (CORS 미발생, 기존 Shell 규약 동일)
- 401 응답 시 Axios 인터셉터가 Shell의 재로그인 플로우로 위임 (Remote가 직접 리다이렉트하지 않음)
- CSRF: 상태 변경 요청(POST/PATCH/PUT/DELETE)에 `X-XSRF-TOKEN` 헤더 부착, 백엔드 Double Submit Cookie 검증

### 7.6 빌드 및 배포

```bash
yarn install --immutable
yarn type-check && yarn lint
yarn build                       # dist/ + remoteEntry.js 산출
```

- 산출물: 정적 자산 → Nginx 컨테이너 이미지로 패키징, K8s Pod x2
- `remoteEntry.js`는 **캐시 무효화 필수** (`Cache-Control: no-cache`), 그 외 해시 파일명 자산은 장기 캐시
- 환경 변수는 빌드 시 주입하지 않고 런타임 `/config.json` 조회 방식 사용 (이미지 1개로 전 환경 배포)
- 번들 예산: 초기 로드 gzip 250KB 이하, Swagger UI·ECharts는 동적 `import()` 로 지연 로드

---

## 8. 카탈로그 등록 → 라우트 자동 생성 파이프라인

```mermaid
%%{init: {'theme':'neutral'}}%%
flowchart LR
    A["API 오너<br/>카탈로그 등록"] --> B["OpenAPI 스펙 검증<br/>(필수 필드·Scope 정의)"]
    B --> C{"검증 통과?"}
    C -- No --> A
    C -- Yes --> D["status=PUBLISHED<br/>DB 저장"]
    D --> E["APISIX Admin API 호출<br/>PUT /apisix/admin/routes/{id}"]
    E --> F["Upstream 등록<br/>+ 플러그인 설정 주입"]
    F --> G["Smoke Test<br/>(테스트 PAT 로 1회 호출)"]
    G --> H{"200 응답?"}
    H -- No --> I["롤백: 라우트 삭제<br/>status=DRAFT 복귀"]
    H -- Yes --> J["카탈로그 공개 완료<br/>알림 발송"]
```

- APISIX Admin API 인증: `X-API-KEY` (K8s Secret 관리, 포털 백엔드만 접근)
- 라우트 ID 규칙: `capi-{api_code 소문자}-{major version}` (예: `capi-mdm-vendor-v1`)
- 멱등성: 동일 ID에 대해 PUT 사용, 변경 전 스냅샷을 DB에 보관하여 롤백 지원

---

## 9. 배치 작업 스펙

| 배치 ID | 주기 | 처리 내용 | 실패 시 |
| :--- | :--- | :--- | :--- |
| `BAT-CDP-01` | 매일 00:05 | 전일 감사 로그 → `usage_stat_daily` 집계 | 재시도 3회, 실패 시 알림 |
| `BAT-CDP-02` | 매일 08:00 | 만료 D-7 / D-1 PAT 알림 발송 | 스킵 후 다음일 재발송 |
| `BAT-CDP-03` | 매일 02:00 | `expires_at` 경과 PAT → `EXPIRED`, Redis 정리 | 재시도 3회 |
| `BAT-CDP-04` | 매일 02:30 | 30일 미사용 PAT → `INACTIVE` | 재시도 3회 |
| `BAT-CDP-05` | 매일 03:00 | 인사 연동 — 퇴직·휴직자 PAT 강제 `REVOKED` | 필수, 실패 시 즉시 알림 |
| `BAT-CDP-06` | 매월 1일 01:00 | 감사 로그 파티션 생성(+2개월), 1년 초과분 아카이브 | 필수 |
| `BAT-CDP-07` | 분기 1회 | 권한 재인증(Recertification) 대상 통보 | 관리자 확인 |

- 구현: Spring Batch + 사내 `common-batch-starter` 공통 인프라 적용, 분산 락(Redis)으로 중복 실행 방지

---

## 10. 관측성(Observability) 스펙

### 10.1 지표 (Prometheus)
| 지표명 | 타입 | 라벨 | 용도 |
| :--- | :--- | :--- | :--- |
| `cdp_api_requests_total` | Counter | `token_id`, `api_code`, `status` | 호출량·오류율 |
| `cdp_api_latency_seconds` | Histogram | `api_code` | P50/P95/P99 |
| `cdp_token_exchange_total` | Counter | `result`(hit/miss/fail) | 캐시 적중률 |
| `cdp_token_exchange_latency_seconds` | Histogram | - | 교환 지연 |
| `cdp_quota_rejected_total` | Counter | `token_id`, `reason` | 쿼터 차단 현황 |
| `cdp_active_pat_count` | Gauge | `status` | PAT 현황 |

### 10.2 알람 규칙
| 알람 | 조건 | 등급 |
| :--- | :--- | :--- |
| 토큰 교환 실패율 | 5분 평균 > 5% | Critical |
| JWT 캐시 적중률 저하 | 15분 평균 < 80% | Warning |
| 쿼터 소진 임박 | 특정 PAT 일일 쿼터 90% 도달 | Info (소유자 통보) |
| Gateway 5xx | 5분간 > 1% | Critical |
| 이상 호출 패턴 | 직전 7일 평균 대비 10배 초과 | Warning (보안 검토) |
| PAT 인증 실패 급증 | 동일 tokenId 1분 내 401 20회 이상 | Warning (탈취 의심) |

### 10.3 분산 추적
- OpenTelemetry W3C `traceparent` 전파: 시민 GW → TXS → 내부 GW → PDP → 서비스
- `X-Request-Id` = trace_id 하위 32자, 오류 응답에 포함하여 CS 대응에 활용

---

## 11. 테스트 계획

### 11.1 기능 테스트 케이스 (발췌)
| TC ID | 시나리오 | 기대 결과 |
| :--- | :--- | :--- |
| TC-A-01 | 유효 PAT로 허용 API 호출 | 200, 쿼터 헤더 정상 |
| TC-A-02 | 폐기된 PAT 호출 | 401 `CDP-1001`, 감사 로그 기록 |
| TC-A-03 | 만료된 PAT 호출 | 401 `CDP-1001` |
| TC-A-04 | Scope 외 경로 호출 | 403 `CDP-1003` |
| TC-A-05 | 허용 CIDR 외 IP 호출 | 403 `CDP-1006` |
| TC-Q-01 | TPS 초과 (11 req/s, 한도 10) | 429 + `Retry-After` |
| TC-Q-02 | 일일 쿼터 소진 후 호출 | 429 `CDP-1004` |
| TC-Q-03 | 자정 경과 후 쿼터 리셋 확인 | 정상 호출 복귀 |
| TC-X-01 | Keycloak 중단 상태 호출 (캐시 있음) | 200 (캐시 JWT 사용) |
| TC-X-02 | Keycloak 중단 + 캐시 없음 | 503 `CDP-2001` |
| TC-X-03 | 교환 JWT의 aud/scope 검증 | 부여 Scope만 포함 |
| TC-P-01 | 미승인 신청으로 PAT 발급 시도 | 403 `CDP-4003` |
| TC-P-02 | 활성 PAT 4개째 발급 | 409 `CDP-4009` |
| TC-P-03 | 폐기 직후 재호출 | 401 (5초 이내 반영) |

### 11.2 보안 테스트
- PAT 탈취 시나리오: 로그·APM·에러응답 전수 검색으로 평문 노출 0건 확인
- 헤더 스푸핑: 클라이언트가 `Authorization: Bearer <임의 JWT>`, `X-Citizen-PAT-Id` 주입 시 무시·차단 확인
- 권한 상승: PAT Scope 외 내부 관리 경로(`/api/internal/**`) 호출 차단 확인
- 쿼터 우회: 동일 PAT 다중 Pod 동시 호출 시 Redis 원자 카운터 정확성 검증 (오차 0)
- 토큰 열거: tokenId 무작위 대입 시 Rate Limit 및 Negative Cache 동작 확인

### 11.3 성능 테스트 (k6)
| 시나리오 | 부하 | 합격 기준 |
| :--- | :--- | :--- |
| 캐시 적중 정상 호출 | 500 VU / 10분 | P95 ≤ 50ms(GW 구간), 오류율 < 0.1% |
| 캐시 미스 집중 | 100 VU / 5분 | P95 ≤ 250ms, 교환 실패 0건 |
| 쿼터 경계 부하 | 2,000 TPS 순간 | 429 정확 반환, 5xx 0건 |
| 장애 주입 (Keycloak 지연 5s) | 100 VU | 서킷 OPEN 30초 내 동작, 5xx 비율 안정화 |

---

## 12. 배포 및 운영

### 12.1 환경 구성
| 환경 | 용도 | Keycloak Realm | 비고 |
| :--- | :--- | :--- | :--- |
| DEV | 개발·단위 테스트 | `hd-dev` | 쿼터 한도 완화 |
| STG | 통합·성능·보안 테스트 | `hd-stg` | 운영 동일 구성(축소) |
| PRD | 운영 | `hd` | 이중화, 감사 로그 1년 보존 |

### 12.2 배포 파이프라인
1. GitLab CI: 빌드 → 단위테스트 → SAST(SonarQube) → 컨테이너 스캔(Trivy)
2. 이미지 레지스트리 푸시 → Helm Chart 버전 태깅
3. ArgoCD GitOps 동기화 (DEV 자동 / STG·PRD 승인)
4. APISIX 플러그인 배포: ConfigMap 갱신 후 Rolling Restart, 카나리 1 Pod 선반영
5. 롤백: Helm 이전 리비전 복구, APISIX 라우트 스냅샷 복원

### 12.3 운영 절차
| 상황 | 조치 |
| :--- | :--- |
| PAT 탈취 의심 | 관리자 즉시 폐기 → Redis 캐시 삭제 확인 → 감사 로그로 피해 범위 산정 → 소유자 통보 |
| 특정 API 과부하 | 해당 라우트 쿼터 일괄 하향 또는 라우트 일시 비활성(`status=0`) |
| Keycloak 점검 | 사전 공지 후 JWT 캐시 TTL 임시 연장(240s→600s), 점검 중 신규 교환 차단 |
| 쿼터 상향 요청 | 포털 신청 → 관리자 검토(실사용량 근거) → `PUT /tokens/{id}/quota` 즉시 반영 |

---

## 13. 구현 체크리스트

### 13.1 Phase 1 (PoC)
- [ ] Keycloak 대상 버전의 Token Exchange 기능 활성 및 동작 확인 (RK-01 판정)
- [ ] `citizen-gw-exchanger` Client 생성 및 Impersonation 권한 부여
- [ ] 다운스코핑(`scope` 파라미터) 반영 여부 검증 — 요청 Scope만 JWT에 포함되는지
- [ ] 교환 JWT를 기존 내부 GW가 정상 검증·인가하는지 확인
- [ ] APISIX + Redis Cluster 기동, Lua 쿼터 스크립트 원자성 검증

### 13.2 Phase 2 (코어)
- [ ] DDL 적용 및 마이그레이션 스크립트(Flyway) 작성
- [ ] PAT 생성·해시·1회 노출 로직 및 단위 테스트
- [ ] `pat-auth` / `pat-quota` / `pat-audit` 플러그인 구현 및 통합 테스트
- [ ] Vue 3 포털 화면 10종 구현 및 MFE Shell 통합(`shared` 싱글톤 검증)
- [ ] PAT 발급 다이얼로그 보안 UX 7개 요건 구현 및 검증
- [ ] 카탈로그 등록 → APISIX Admin 라우트 자동 생성·롤백 검증

### 13.3 Phase 3 (검증)
- [ ] 11장 기능/보안/성능 테스트 전 항목 수행 및 결함 조치
- [ ] 로그 전수 검색으로 PAT 평문 노출 0건 확인
- [ ] Prometheus 지표·알람 규칙 등록, Grafana 대시보드 3종 구성
- [ ] 장애 훈련(Keycloak·Redis 중단) 시나리오 수행

### 13.4 Phase 4 (오픈)
- [ ] 파일럿 3개 API 공개 및 파일럿 부서 온보딩
- [ ] 시민개발자 가이드(호출 예제: cURL / Python / Power Automate / Excel VBA) 배포
- [ ] 운영 이관 문서 및 장애 대응 매뉴얼 작성
- [ ] 분기 권한 재인증 프로세스 정식 등록

---

## 부록 A. 호출 예제

### A.1 cURL
```bash
curl -X GET "https://capi.hd.com/capi/v1/vendors?page=1&size=50" \
  -H "Authorization: Bearer hdpat_a7Kd93mQx2Lp_Xk8sT2vQmN4rZp1LwEu6Yb3Hc9Ad5Jf0Kg2" \
  -H "Accept: application/json"
```

### A.2 Python
```python
import os, requests

TOKEN = os.environ["HD_CAPI_TOKEN"]   # 코드에 하드코딩 금지
resp = requests.get(
    "https://capi.hd.com/capi/v1/vendors",
    headers={"Authorization": f"Bearer {TOKEN}"},
    params={"page": 1, "size": 50},
    timeout=30,
)
if resp.status_code == 429:
    wait = int(resp.headers.get("Retry-After", "30"))
    print(f"쿼터 초과 — {wait}초 후 재시도")
resp.raise_for_status()
print(resp.json(), resp.headers.get("X-RateLimit-Remaining"))
```

### A.3 오류 처리 권고
| 상태 | 권고 조치 |
| :--- | :--- |
| 401 | 토큰 재발급 필요. 자동 재시도 금지 |
| 403 | 포털에서 Scope 추가 신청 |
| 429 | `Retry-After` 준수, 지수 백오프 적용 |
| 503 | 30초 후 재시도, 3회 실패 시 중단 및 담당자 통보 |

## 부록 B. 설정 파라미터 기본값

| 파라미터 | 기본값 | 조정 범위 |
| :--- | :--- | :--- |
| PAT 유효기간 | 90일 | 1~180일(예외 승인) |
| 사용자당 활성 PAT | 10개 | 1~20 |
| 신청 건당 활성 PAT | 3개 | 1~5 |
| Rate Limit | 10 TPS / burst 20 | 1~100 |
| Daily Quota | 5,000 | 100~500,000 |
| Monthly Quota | 100,000 | 1,000~10,000,000 |
| 동시 요청 | 5 | 1~50 |
| JWT TTL | 300초 | 60~600 |
| JWT 캐시 TTL | 240초 | JWT TTL의 80% |
| 미사용 회수 기준 | 30일 | 14~90일 |
| 감사 로그 보존 | 1년 | 규정 준수 |

---

## 부록 C. Keycloak Token Exchange 구축 매뉴얼

> **본 부록은 Phase 1 PoC의 실행 지침서입니다.** 아래 C.1의 버전 판정을 반드시 먼저 수행한 후 C.3~C.6 중 해당 방식만 적용하십시오.

### C.1 사전 판정 — Keycloak 버전별 지원 현황 (필독)

Keycloak 26.2(2025-04) 이후 Token Exchange는 **2개 구현이 공존**하며, 본 설계가 요구하는 기능은 둘 중 하나에만 존재합니다.

| 구분 | Standard Token Exchange (V2) | Legacy Token Exchange (V1) |
| :--- | :--- | :--- |
| 상태 | **정식 지원(Supported)**, 26.2부터 기본 활성 | **Preview + Deprecated**, 향후 제거 예정 |
| 기능 플래그 | 불요 (클라이언트 스위치만 On) | `--features=token-exchange` (26.2+ 는 `token-exchange:v1`) |
| 부가 요건 | 없음 | FGAP v1 (`admin-fine-grained-authz`) 필요 |
| 지원 범위 | internal → internal 교환만 | internal→internal, external→internal, **사용자 임퍼소네이션** |
| `subject_token` | **필수** | 임퍼소네이션 시 불요 |
| `requested_subject` | **미지원 (Not implemented yet)** | **지원** |
| 인가 방식 | `aud` 클레임 일치로 검증, 별도 권한 설정 불요 | 대상 Client·IdP에 `token-exchange` 권한 명시 정의 |
| `audience` 의미 | 다운스코핑·롤 필터링 | 단일 대상 Client 지정 |

**본 설계의 요구는 "Use Case 4 — 클라이언트가 사용자를 임퍼소네이션(direct naked impersonation)"에 해당합니다.**
시민개발자는 브라우저 세션이 없어 **사용자 본인의 `subject_token`이 존재하지 않으므로**, V2 표준 교환만으로는 구현할 수 없습니다.

**판정 절차**
```bash
# 1) 운영 Keycloak 버전 확인
curl -s https://sso.hd.com/admin/serverinfo \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r '.systemInfo.version'

# 2) 활성 기능 플래그 확인
curl -s https://sso.hd.com/admin/serverinfo \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r '.features[] | select(.name|test("TOKEN_EXCHANGE|FINE_GRAINED")) | "\(.name) = \(.enabled)"'

# 3) 토큰 엔드포인트 지원 grant_type 확인
curl -s https://sso.hd.com/realms/hd/.well-known/openid-configuration \
  | jq -r '.grant_types_supported[]' | grep token-exchange
```

**판정 결과별 분기**

| 조건 | 적용 방식 | 절 |
| :--- | :--- | :--- |
| V1 활성화가 조직 정책상 허용됨 (PoC·단기) | 방식 A — Legacy V1 임퍼소네이션 | C.3 |
| V1 사용 불가 / 제거 대비 필요 | 방식 B — Custom Protocol Mapper + Client Credentials | C.4 |
| 사용자 사전 동의 절차 도입 가능 | 방식 C — V2 Delegation (실험적) | C.5 |
| Keycloak 의존 최소화 우선 | 방식 D — TXS 자체 서명 + 내부 GW 신뢰 등록 | C.6 |

> **권고**: Phase 1은 **방식 A로 PoC를 신속히 검증**하되, 운영 적용은 **방식 B를 기본 설계로 채택**합니다. V1이 Deprecated이며 제거가 예고되어 있고, 26.5.x에서 `UserPermissionsV2.canClientImpersonate` NPE 등 버전 간 회귀가 보고되어 장기 운영 리스크가 높기 때문입니다.

---

### C.2 공통 설정 — Exchanger 클라이언트 생성

모든 방식에 공통으로 필요한 서비스 계정 클라이언트를 먼저 구성합니다.

```bash
# --- 관리자 로그인 ---
export KC=/opt/keycloak/bin
$KC/kcadm.sh config credentials \
  --server https://sso.hd.com \
  --realm master --user svc-cdp-admin

export REALM=hd

# --- Exchanger 클라이언트 생성 ---
$KC/kcadm.sh create clients -r $REALM \
  -s clientId=citizen-gw-exchanger \
  -s name="시민개발자 게이트웨이 토큰 교환기" \
  -s enabled=true \
  -s protocol=openid-connect \
  -s publicClient=false \
  -s serviceAccountsEnabled=true \
  -s standardFlowEnabled=false \
  -s implicitFlowEnabled=false \
  -s directAccessGrantsEnabled=false \
  -s 'attributes."access.token.lifespan"=300' \
  -s 'attributes."client_credentials.use_refresh_token"=false'

# --- 클라이언트 UUID 및 시크릿 확보 ---
export CID=$($KC/kcadm.sh get clients -r $REALM -q clientId=citizen-gw-exchanger \
             --fields id --format csv --noquotes)
$KC/kcadm.sh get clients/$CID/client-secret -r $REALM
# → {"type":"secret","value":"xxxxxxxx"} : K8s Secret 으로 관리
```

**클라이언트 설정 요약**

| 항목 | 값 | 사유 |
| :--- | :--- | :--- |
| Client authentication | On (Confidential) | 표준/레거시 모두 Confidential 필수 |
| Service accounts roles | On | 교환기 자체 인증용 |
| Standard flow | Off | 브라우저 로그인 불필요, 공격면 축소 |
| Direct access grants | Off | 패스워드 그랜트 차단 |
| Access Token Lifespan | 300초 | 캐시 TTL 240초와 짝 |
| 인증 방식 | `client_secret_basic` 또는 **mTLS 권장** | 운영은 `client_jwt`/mTLS 권고 |

**Scope 사전 등록** (공개 API별 1회)
```bash
for S in capi.vendor.read capi.vendor.write capi.order.read; do
  $KC/kcadm.sh create client-scopes -r $REALM \
    -s name=$S -s protocol=openid-connect \
    -s 'attributes."include.in.token.scope"=true' \
    -s 'attributes."display.on.consent.screen"=false'
done

# 교환기 클라이언트에 Optional Scope 로 부착
for S in capi.vendor.read capi.vendor.write capi.order.read; do
  SID=$($KC/kcadm.sh get client-scopes -r $REALM -q name=$S --fields id --format csv --noquotes)
  $KC/kcadm.sh update clients/$CID/optional-client-scopes/$SID -r $REALM
done
```

**Audience Mapper 추가** (내부 GW가 `aud` 검증하므로 필수)
```bash
$KC/kcadm.sh create clients/$CID/protocol-mappers/models -r $REALM \
  -s name=internal-gw-audience \
  -s protocol=openid-connect \
  -s protocolMapper=oidc-audience-mapper \
  -s 'config."included.client.audience"=internal-api-gateway' \
  -s 'config."access.token.claim"=true' \
  -s 'config."id.token.claim"=false'
```

**채널 식별 Claim 추가** (내부 PDP가 시민개발자 채널을 구분해 정책 적용)
```bash
$KC/kcadm.sh create clients/$CID/protocol-mappers/models -r $REALM \
  -s name=cdp-channel \
  -s protocol=openid-connect \
  -s protocolMapper=oidc-hardcoded-claim-mapper \
  -s 'config."claim.name"=cdp_channel' \
  -s 'config."claim.value"=citizen' \
  -s 'config."jsonType.label"=String' \
  -s 'config."access.token.claim"=true'
```

---

### C.3 방식 A — Legacy V1 임퍼소네이션 (PoC 권장)

#### C.3.1 서버 기능 플래그 활성화

```bash
# Keycloak 26.2 미만
bin/kc.sh start --features=token-exchange,admin-fine-grained-authz

# Keycloak 26.2 이상 (V2 와 공존, V1 을 명시적으로 지정)
bin/kc.sh start --features=token-exchange:v1,admin-fine-grained-authz:v1
```

Kubernetes(Keycloak Operator) 적용 예
```yaml
apiVersion: k8s.keycloak.org/v2alpha1
kind: Keycloak
metadata:
  name: hd-sso
spec:
  instances: 3
  features:
    enabled:
      - token-exchange:v1
      - admin-fine-grained-authz:v1
```

> **주의**: 기능 플래그 변경은 **Keycloak 전체 재기동**이 필요합니다. 운영 반영 시 무중단 롤링 업데이트 절차와 사전 공지가 필수입니다.

#### C.3.2 임퍼소네이션 권한 부여

```bash
# 1) Realm 사용자 관리 권한 활성화
$KC/kcadm.sh update users-management-permissions -r $REALM -s enabled=true

# 2) realm-management 클라이언트 UUID 확보
export RM=$($KC/kcadm.sh get clients -r $REALM -q clientId=realm-management \
            --fields id --format csv --noquotes)

# 3) 교환기 클라이언트를 주체로 하는 Client Policy 생성
$KC/kcadm.sh create clients/$RM/authz/resource-server/policy/client -r $REALM \
  -s name=cdp-exchanger-policy \
  -s description="시민개발자 게이트웨이 교환기만 임퍼소네이션 허용" \
  -s 'clients=["'$CID'"]' \
  -s logic=POSITIVE

export POL=$($KC/kcadm.sh get clients/$RM/authz/resource-server/policy \
             -r $REALM -q name=cdp-exchanger-policy --fields id --format csv --noquotes)

# 4) impersonation 권한(Scope Permission) 조회 후 정책 연결
export PERM=$($KC/kcadm.sh get clients/$RM/authz/resource-server/permission \
              -r $REALM -q name=admin-impersonating.permission.users \
              --fields id --format csv --noquotes)

$KC/kcadm.sh update clients/$RM/authz/resource-server/permission/scope/$PERM \
  -r $REALM -s 'policies=["'$POL'"]' -s decisionStrategy=UNANIMOUS
```

관리 콘솔 경로(동일 작업)
```
Users → Permissions 탭 → Permissions enabled = On
  → impersonate 링크 클릭
  → Create client policy → Clients = citizen-gw-exchanger
  → impersonation permission 에 해당 policy 적용
```

> **보안 유의**: 이 설정은 교환기 클라이언트에 **Realm 전체 사용자에 대한 임퍼소네이션 권한**을 부여합니다. 관리자 계정까지 포함되므로, 다음 통제를 반드시 병행하십시오.
> - TXS는 **PAT에 바인딩된 `user_sub`만** `requested_subject`로 전달 (코드 레벨 강제, 요청 파라미터 무검증 전달 금지)
> - 관리자 그룹(`/admins`, `/sso-operators`) 소속 사용자는 **PAT 발급 자체를 차단** (포털 백엔드 검증)
> - 교환기 클라이언트 시크릿은 K8s Secret + 분기별 로테이션
> - 교환 요청 전량 감사 로그 기록

#### C.3.3 교환 요청 (방식 A)

```bash
# 1) 교환기 자체 토큰 획득 (client_credentials)
SUBJ=$(curl -s -X POST \
  https://sso.hd.com/realms/hd/protocol/openid-connect/token \
  -u "citizen-gw-exchanger:${CLIENT_SECRET}" \
  -d "grant_type=client_credentials" | jq -r .access_token)

# 2) 사용자 임퍼소네이션 교환
curl -s -X POST \
  https://sso.hd.com/realms/hd/protocol/openid-connect/token \
  -u "citizen-gw-exchanger:${CLIENT_SECRET}" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=${SUBJ}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "requested_subject=u-10233" \
  -d "requested_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "audience=internal-api-gateway" \
  -d "scope=capi.vendor.read"
```

정상 응답
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6...",
  "expires_in": 300,
  "token_type": "Bearer",
  "scope": "capi.vendor.read",
  "issued_token_type": "urn:ietf:params:oauth:token-type:access_token"
}
```

**교환 결과 JWT 필수 검증 항목** (PoC 합격 기준)
```json
{
  "iss": "https://sso.hd.com/realms/hd",
  "aud": "internal-api-gateway",
  "sub": "u-10233",
  "azp": "citizen-gw-exchanger",
  "scope": "capi.vendor.read",
  "cdp_channel": "citizen",
  "realm_access": { "roles": ["..."] },
  "exp": 1758200400,
  "jti": "8f2a..."
}
```

| 검증 항목 | 합격 기준 | 불합격 시 조치 |
| :--- | :--- | :--- |
| `sub` | PAT 소유자 user_sub 와 일치 | `requested_subject` 값 확인 |
| `aud` | `internal-api-gateway` 포함 | Audience Mapper 재설정 (C.2) |
| `scope` | **요청한 Scope만** 포함, 초과 없음 | 다운스코핑 실패 — Optional Scope 설정 확인 |
| `azp` | `citizen-gw-exchanger` | — |
| `realm_access.roles` | 사용자 원본 롤 유지 | PDP 정책 재활용 가능 여부 판단 근거 |
| `exp - iat` | 300초 | Client Override 확인 |

> **다운스코핑 검증이 PoC의 핵심입니다.** 사용자가 보유한 전체 롤이 그대로 실린다면 "최소 권한 원칙" 요건(R-08)이 깨집니다. 이 경우 내부 PDP 측에서 `scope` 클레임 기반 2차 필터를 추가하는 보완 설계가 필요합니다.

---

### C.4 방식 B — Custom Protocol Mapper + Client Credentials (운영 권장)

V1 제거 리스크를 회피하는 정식 지원 기능 조합입니다. **사용자를 임퍼소네이션하지 않고, 교환기 서비스 계정 토큰에 대상 사용자 식별자를 클레임으로 주입**합니다.

#### C.4.1 구조

```
TXS → client_credentials 요청 (+ 사용자 식별 정보 전달)
    → Keycloak: Custom Protocol Mapper 가 cdp_user_sub / cdp_user_roles 주입
    → JWT: sub = 서비스계정, act.sub = 실제 사용자
    → 내부 PDP: 주체 해석 규칙을 sub → cdp_user_sub 로 확장
```

#### C.4.2 Custom Protocol Mapper SPI (구현 필요 · 약 200 LOC)

```java
public class CitizenUserClaimMapper extends AbstractOIDCProtocolMapper
        implements OIDCAccessTokenMapper {

    public static final String PROVIDER_ID = "cdp-citizen-user-mapper";

    @Override
    protected void setClaim(IDToken token, ProtocolMapperModel mapping,
                            UserSessionModel session, KeycloakSession ks,
                            ClientSessionContext ctx) {

        // TXS 가 전달한 사용자 식별자 (client note 또는 요청 파라미터)
        String userSub = ctx.getClientSession().getNote("cdp_user_sub");
        if (userSub == null) return;

        UserModel user = ks.users().getUserById(ks.getContext().getRealm(), userSub);
        if (user == null || !user.isEnabled()) {
            throw new ErrorResponseException("invalid_request",
                "Unknown or disabled user", Response.Status.BAD_REQUEST);
        }

        token.getOtherClaims().put("cdp_user_sub", userSub);
        token.getOtherClaims().put("cdp_user_name", user.getUsername());
        token.getOtherClaims().put("cdp_channel", "citizen");

        // 위임 표준 형식 (RFC 8693 act 클레임)
        token.getOtherClaims().put("act", Map.of("sub", userSub));

        // 사용자 롤을 요청 Scope 범위로 축소 주입
        Set<String> granted = resolveScopedRoles(user, ctx.getClientScopes());
        token.getOtherClaims().put("cdp_user_roles", granted);
    }
}
```

배포
```bash
# JAR 빌드 후 providers 디렉터리 배치
cp cdp-citizen-mapper-1.0.0.jar /opt/keycloak/providers/
bin/kc.sh build        # 프로바이더 재인덱싱 (필수)
bin/kc.sh start
```

Mapper 부착
```bash
$KC/kcadm.sh create clients/$CID/protocol-mappers/models -r $REALM \
  -s name=cdp-citizen-user \
  -s protocol=openid-connect \
  -s protocolMapper=cdp-citizen-user-mapper \
  -s 'config."access.token.claim"=true'
```

#### C.4.3 장단점

| 구분 | 내용 |
| :--- | :--- |
| 장점 | Preview 기능 무의존, 버전 업그레이드 안전, 임퍼소네이션 권한 불요(권한 과다 부여 회피), `act` 클레임으로 위임 관계 명시 |
| 단점 | **Keycloak SPI 커스텀 개발·유지보수 필요**, Keycloak 업그레이드 시 SPI 호환성 검증 필요 |
| 내부 영향 | **내부 PDP에 주체 해석 규칙 1건 추가 필수** — `cdp_channel=citizen` 인 경우 주체를 `cdp_user_sub`로 해석 |

> 내부 PDP 변경이 발생하므로, "기존 인가 서비스 100% 무변경" 원칙에서 **규칙 1건 예외**가 생깁니다. 이 예외를 수용할지 여부가 방식 A/B 선택의 핵심 의사결정 포인트입니다.

---

### C.5 방식 C — V2 Delegation (참고 · 실험적)

Keycloak이 RFC 8693 위임 의미론으로 제공하는 기능으로, **사용자가 사전에 액터를 승인**하는 모델입니다.

```bash
bin/kc.sh start --features=token-exchange-delegation,parameterized-scopes
```

- `delegation:user` Client Scope가 전 Realm에 Optional로 자동 생성됨
- 요청 예: `scope=openid delegation:user:citizen-gw-exchanger`
- 사용자 속성의 `may_act` 클레임에 액터를 등록해야 교환 허용

**본 설계 적용 평가**: 시민개발자가 PAT 발급 시점에 포털에서 명시적으로 위임을 승인하는 흐름과 의미론적으로 부합합니다. 다만 **Experimental 등급**이며 `parameterized-scopes` 동시 활성이 필요해 운영 도입은 시기상조입니다. **Phase 3 재평가 항목**으로 등록합니다.

---

### C.6 방식 D — TXS 자체 서명 (최후 대안)

Keycloak Token Exchange를 사용하지 않고, TXS가 자체 키페어로 JWT를 서명해 내부 GW가 신뢰하도록 등록하는 방식입니다.

- 내부 GW의 신뢰 발급자 목록에 `https://cdp-txs.hd.com` 추가, JWKS 엔드포인트 등록
- TXS는 PAT 정보 기반으로 `sub`, `scope`, `roles` 클레임을 직접 구성
- 사용자 롤은 Keycloak Admin REST API로 조회(캐시 5분)

| 구분 | 내용 |
| :--- | :--- |
| 장점 | Keycloak 기능 의존 완전 제거, 교환 지연 제거(150~200ms → 0), IdP 장애 영향 최소화 |
| 단점 | **신뢰 발급자가 2개로 증가** — 통합 인증 아키텍처 원칙 훼손, 키 관리·회전 책임 자체 부담, 롤 정보 정합성 지연 |
| 판단 | 아키텍처 표준 위배 소지가 커 **권고하지 않음**. V1 제거 + 방식 B 불가 시에만 검토 |

---

### C.7 트러블슈팅

| 증상 / 오류 | 원인 | 조치 |
| :--- | :--- | :--- |
| `unsupported_grant_type` | 기능 플래그 미활성 | C.3.1 플래그 확인 후 재기동. `.well-known`에서 grant_type 노출 여부 재확인 |
| `invalid_client` | 시크릿 불일치 / Public Client | Confidential 여부, 시크릿 로테이션 반영 확인 |
| `403 Forbidden` + `Client not allowed to exchange` | 임퍼소네이션 권한 미부여 | C.3.2의 Client Policy ↔ Permission 연결 재확인 |
| `400 invalid_request` + `Invalid token type` | `subject_token_type` 누락/오기 | `urn:ietf:params:oauth:token-type:access_token` 정확히 전달 |
| NPE `UserPermissionsV2.canClientImpersonate` | 26.5.x + FGAP v2 조합 회귀 | `admin-fine-grained-authz:v1` 명시 활성 (커뮤니티 확인 워크어라운드) |
| JWT에 `aud` 없음 → 내부 GW 401 | Audience Mapper 미설정 | C.2 Audience Mapper 추가 |
| 요청하지 않은 Scope까지 포함 | Scope가 Default로 부착됨 | Optional Client Scope로 변경, `scope` 파라미터 명시 |
| `scope` 파라미터 무시됨 | 해당 Scope 미등록 또는 Client 미부착 | C.2 Scope 등록·부착 재확인 |
| 교환 지연 500ms 초과 | Keycloak DB 커넥션 풀 부족 / User 조회 병목 | `db-pool-max-size` 상향, Keycloak 인스턴스 증설, 캐시 TTL 상향 |
| 간헐적 401 (드물게) | 캐시 TTL과 JWT exp 경계 | 캐시 TTL을 JWT TTL의 80% 이하 유지 (현 240/300) |

### C.8 PoC 판정 체크리스트 (Phase 1 종료 조건)

- [ ] 운영 예정 Keycloak 버전 확정 및 V1/V2 지원 현황 판정 완료
- [ ] 방식 A로 교환 성공, JWT 필수 검증 항목 6종 전부 합격 (C.3.3 표)
- [ ] **다운스코핑 동작 확인** — 요청 Scope 외 권한 미포함
- [ ] 교환 JWT를 기존 내부 GW가 무변경으로 검증·인가 성공
- [ ] 내부 PDP 정책 무변경 재사용 가능 여부 판정 (불가 시 방식 B 확정)
- [ ] 교환 지연 실측 (목표 P95 ≤ 200ms)
- [ ] V1 Deprecated 리스크에 대한 운영 방식 결정 — A 유지 / B 전환 / C 재평가
- [ ] 관리자 그룹 사용자 PAT 발급 차단 로직 검증
- [ ] Keycloak 기능 플래그 변경에 따른 기존 서비스 영향도 점검 (재기동 필요)

