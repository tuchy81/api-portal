# 시민개발자 API 포털 — API/Endpoint 모델 최종 개발 스펙

- 문서 ID: SA-CHG-CDP-API-FINAL
- 대상 브랜치: `apisix`
- 통합 대상 스펙:
  - SA-CHG-CDP-API-001 — API 등록 Endpoint-Upstream 매핑 변경
  - Endpoint 명칭 전환 개발 스펙
  - API Endpoint 중복 및 Route 충돌 방지 변경 개발 스펙
- 상태: **구현 완료** (본 문서는 개발 완료 후 최종본)
- 작성 기준일: 2026-09-28

이 문서는 위 세 스펙의 결정 사항과 그에 따른 실제 구현 상태를 하나로 정리한다. 개별 스펙 문서는 변경 배경/근거 자료로 보존하되, 본 문서를 단일 소스로 참조한다.

---

## 1. 목표와 원칙

### 1.1 통합 목표

1. **API vs Endpoint의 축을 분리한다.** API는 시민개발자에게 공개되는 하나의 논리 묶음(Public Base 하나), Endpoint는 실제 호출 단위(Method + Path Pattern + Upstream Full URL).
2. **Endpoint별 Upstream을 명시한다.** 하나의 Public API가 서로 다른 Backend Service/Resource와 자유롭게 조합될 수 있게 한다.
3. **Endpoint 명칭을 명확히 한다.** `api_scope` 테이블 명이 사실상 Endpoint를 의미했으므로 `api_endpoint`로 정정한다.
4. **Endpoint의 필수 권한(Scope)을 분리하여 명명한다.** 기존 `scope_name` 필드는 표시 이름이 아니라 "이 Endpoint 호출에 필요한 OAuth Scope" 참조이므로 `required_scope`로 이름을 바꾼다.
5. **Endpoint 중복을 사전 차단한다.** 파라미터 이름 차이, trailing slash 차이 등 의미상 동일한 Route를 등록 시점부터 canonical 비교로 걸러낸다.
6. **OAuth/PAT Scope의 의미는 유지한다.** application의 requested/granted scope, PAT의 scopes, Keycloak `scope` claim 등 인증 계열의 "scope" 용어는 변경하지 않는다.

### 1.2 최종 개념 모델

```
API (cdp.api_catalog)
  ├─ api_code, name, description, owner_dept, owner_sub
  ├─ public_path        ← Public Base 하나 (예: /capi/v1/vendors)
  ├─ required_roles     ← 신청 자격 role 목록
  ├─ openapi_spec       ← 선택
  ├─ status             ← DRAFT / PUBLISHED / DEPRECATED / RETIRED
  └─ Endpoint[]  (cdp.api_endpoint)
        ├─ endpoint_id
        ├─ http_method
        ├─ path_pattern     ← Public Base에 붙는 상대 경로
        ├─ upstream_url     ← 실제 호출 Backend Full URL
        ├─ required_scope   ← 이 Endpoint 호출에 필요한 OAuth Scope
        └─ description
```

관계:
- **PAT ↔ API**: 1:1 (application.api_id → pat.app_id 체인)
- **API ↔ Endpoint**: 1:N
- **Required Scope ↔ Endpoint**: 1:N (하나의 Scope가 여러 Endpoint에 공유될 수 있음; 예: `capi.vendor.read`가 `GET /`와 `GET /{id}`에 공유)

### 1.3 Scope 유지 결정 근거

Endpoint 단위 Scope 강제는 다음 이유로 유지한다.
- PAT는 하나의 API에 묶이지만, 같은 API 안에서 read/write 분리를 하려면 Endpoint 단위 Scope 매치가 필요하다(pat-auth.lua §264-296).
- 향후 write 계열 API 노출 계획이 있으므로 이 게이트를 유지해야 실제 인가가 강제된다.
- Keycloak IM에는 이 애플리케이션 scope(`capi.vendor.read` 등)가 정의되어 있지 않으며, **Scope 값의 정합성은 Portal(포털)이 단독 관리**한다 (§10.2 참조).

---

## 2. 데이터 모델

### 2.1 최종 스키마

```sql
CREATE TABLE cdp.api_catalog (
    api_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    api_code        VARCHAR(64)  NOT NULL UNIQUE,
    name            VARCHAR(200) NOT NULL,
    description     TEXT,
    owner_dept      VARCHAR(100) NOT NULL,
    owner_sub       VARCHAR(64)  NOT NULL,
    public_path     VARCHAR(200) NOT NULL UNIQUE,
    openapi_spec    JSONB,
    required_roles  TEXT[]       NOT NULL DEFAULT '{}',
    status          VARCHAR(20)  NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT','PUBLISHED','DEPRECATED','RETIRED')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE cdp.api_endpoint (
    endpoint_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    api_id          UUID         NOT NULL REFERENCES cdp.api_catalog(api_id) ON DELETE CASCADE,
    required_scope  VARCHAR(100) NOT NULL,
    http_method     VARCHAR(10)  NOT NULL,
    path_pattern    VARCHAR(300) NOT NULL,
    upstream_url    VARCHAR(500) NOT NULL,
    description     VARCHAR(300),
    UNIQUE (api_id, http_method, path_pattern)
);
```

- `api_catalog.upstream_url` 컬럼은 존재하지 않는다(SA-CHG-CDP-API-001로 제거됨).
- `UNIQUE (api_id, http_method, path_pattern)` 은 원문 문자열 비교이며, 애플리케이션 계층의 canonical 비교와 함께 이중 방어를 이룬다.

### 2.2 이전 스키마 대비 변경 요약

| 이전 | 최종 |
|---|---|
| `cdp.api_scope` | `cdp.api_endpoint` |
| `scope_id` | `endpoint_id` |
| `scope_name` | `required_scope` |
| `cdp.api_catalog.upstream_url` | 삭제됨 (Endpoint별 upstream으로 이전) |

### 2.3 마이그레이션

기존 pgdata 볼륨에 대해 `services/portal-backend/db_migrations.py`가 부팅 시 아래를 idempotent하게 실행한다.

1. `_migrate_endpoint_upstream` (SA-CHG-CDP-API-001)
   - `api_scope.upstream_url` 컬럼이 없으면 추가
   - 시드 API의 `api_scope` 행을 canonical 시드 셋으로 재작성
   - 사용자 등록 API에 대해 기존 API-level upstream + 기존 rewrite 규칙(`/capi/vN(.*) → upstream_path$1`)을 재현하여 Endpoint별 upstream backfill
   - `api_scope.upstream_url` NOT NULL 고정
   - `api_catalog.upstream_url` 삭제
2. `_migrate_scope_table_to_endpoint` (본 통합 스펙)
   - `cdp.api_scope` → `cdp.api_endpoint` RENAME TABLE
   - `scope_id` → `endpoint_id` RENAME COLUMN
   - `scope_name` → `required_scope` RENAME COLUMN
   - UNIQUE/FK/PK 제약은 이름 그대로 따라간다.
   - 이미 신 스키마인 경우 no-op.

`infra/postgres/init/01_ddl.sql`은 신 스키마를 그대로 정의하므로, 빈 볼륨은 마이그레이션 실행 없이 목표 상태에 도달한다.

`infra/postgres/migrations/2026_09_27_endpoint_upstream.sql`은 사람이 수동 실행하는 참조본으로 유지된다. 여기에 `2026_09_28_scope_to_endpoint_rename.sql`을 나중에 동일 형식으로 추가하면 좋다(코드 마이그레이션이 이미 처리하므로 필수는 아님).

---

## 3. Endpoint 등록 규칙

### 3.1 Public Base (`api_catalog.public_path`)

- 반드시 `/` 로 시작
- API 버전을 포함 (`/capi/v1/vendors`)
- Path Parameter 금지 (`{}`, `:`, `*` 문자 불가)
- 상세 기능 경로 미포함 (Endpoint Pattern에 정의)
- DB UNIQUE로 전 시스템 유일

거부 예: `/capi/v1/vendors/{id}`, `/capi/v1/vendors/search`

### 3.2 Endpoint Path Pattern (`api_endpoint.path_pattern`)

- 반드시 `/`로 시작하는 상대 경로
- Public Base를 다시 포함하면 400
- Query string / fragment 금지
- Wildcard(`*`) 금지 (현재 스코프 밖; 필요 시 별도 설계)
- Path Parameter는 `{name}` 또는 `:name` 형태 허용, 이름은 upstream template과 정확히 매칭되어야 함

최종 Public URL = `public_base + path_pattern` (spec §3.2). Pattern이 `/`이면 최종 URL은 Public Base 자체.

### 3.3 Endpoint Upstream URL (`api_endpoint.upstream_url`)

- 절대 URL (scheme + host 필수, fragment 금지)
- Path에 `{name}` 파라미터가 있으면 Path Pattern에도 반드시 선언되어 있어야 함 (그렇지 않으면 proxy-rewrite에서 조용히 사라짐)
- Public Base와 Backend Path 구조가 달라도 허용
- 서로 다른 Endpoint가 서로 다른 Backend Service를 가리켜도 허용 (하나의 Public API 안에서 자유 조합)

### 3.4 Endpoint 중복 판정 (Canonical Route Identity)

**중복 Key = `(HTTP Method, Normalized Path Pattern)`**

Normalized Path Pattern 규칙(`_normalize_endpoint_pattern`, `services/portal-backend/routers/catalog.py`):
1. 반드시 `/` 시작
2. Path Parameter `{name}` / `:name` → 캐노니컬 토큰 `{param}` 로 치환 (이름 무시)
3. Root `/` 외 trailing `/` 제거
4. 연속 slash(`//`) 는 validation error

| 입력 | 정규화 | 비교 결과 |
|---|---|---|
| `GET /vendors` | `/vendors` | 동일 |
| `GET /vendors/` | `/vendors` | 동일 |
| `GET /vendors/{id}` | `/vendors/{param}` | 동일 |
| `GET /vendors/{vendorId}` | `/vendors/{param}` | 동일 |
| `GET /vendors/:id` | `/vendors/{param}` | 동일 |
| `POST /vendors` | `/vendors` | GET과 공존 가능 (Method 다름) |
| `GET /vendors/{id}/orders` | `/vendors/{param}/orders` | 별도 Endpoint |

프론트엔드도 동일 정규화를 수행하여 등록/편집 화면에서 즉시 사용자에게 알린다(§7 참조). 최종 판단은 항상 서버.

---

## 4. Backend API Contract

### 4.1 Request Body (POST `/portal/v1/catalog/apis`)

```json
{
  "apiCode": "MDM-VENDOR",
  "name": "MDM 협력사 API",
  "description": "MDM 협력사 정보 조회/관리",
  "ownerDept": "MDM운영팀",
  "publicPath": "/capi/v1/vendors",
  "requiredRoles": ["citizen-developer", "mdm-reader"],
  "endpoints": [
    {
      "requiredScope": "capi.vendor.read",
      "httpMethod": "GET",
      "pathPattern": "/",
      "upstreamUrl": "http://vendor-service:8080/internal/api/v1/vendors",
      "description": "협력사 목록 조회"
    },
    {
      "requiredScope": "capi.vendor.read",
      "httpMethod": "GET",
      "pathPattern": "/{id}",
      "upstreamUrl": "http://vendor-service:8080/internal/api/v1/vendors/{id}",
      "description": "협력사 단건 조회"
    },
    {
      "requiredScope": "capi.vendor.write",
      "httpMethod": "POST",
      "pathPattern": "/",
      "upstreamUrl": "http://vendor-command:8090/api/vendor/create",
      "description": "협력사 등록"
    }
  ],
  "openapiSpec": { ... }   // optional
}
```

- 상위 배열 키는 `endpoints` (`scopes` 아님)
- Endpoint 항목 필드:
  - `requiredScope` (구 `scopeName`) — 이 Endpoint 호출에 필요한 OAuth Scope
  - `httpMethod`, `pathPattern`, `upstreamUrl`, `description`

### 4.2 PATCH `/portal/v1/catalog/apis/{api_id}`

- `endpoints` 필드가 포함되면 **전체 교체** (DELETE all + INSERT new)
- 다른 필드는 개별 갱신
- 라우팅에 영향 있는 변경(status / publicPath / endpoints) 시 APISIX Route 재구성 + JWT 캐시 무효화

### 4.3 GET `/portal/v1/catalog/apis/{api_id}`

응답에 `endpoints` 배열 포함 (구 `scopes` 아님). 각 원소는 DB 컬럼명(snake_case) 그대로:
```json
{
  "api_id": "...",
  "api_code": "MDM-VENDOR",
  "public_path": "/capi/v1/vendors",
  "endpoints": [
    {
      "endpoint_id": "...",
      "http_method": "GET",
      "path_pattern": "/{id}",
      "upstream_url": "...",
      "required_scope": "capi.vendor.read",
      "description": "..."
    }
  ]
}
```

### 4.4 GET `/portal/v1/catalog/apis/{api_id}/endpoints/check`

프론트엔드 사전 검사용(선택). Query: `httpMethod`, `pathPattern`.
```json
// 사용 가능
{ "available": true, "normalizedPattern": "/{param}" }

// 충돌
{
  "available": false,
  "normalizedPattern": "/{param}",
  "conflict": {
    "requiredScope": "capi.vendor.read",
    "httpMethod": "GET",
    "pathPattern": "/{id}"
  }
}
```
UI 최종 보장이 아니며, 실제 등록 시 서버가 동일 canonical 비교를 다시 수행한다.

### 4.5 오류 응답 (RFC 9457 Problem+JSON)

| 코드 | HTTP | 사유 |
|---|---|---|
| `CDP-4002` | 400 | Catalog 입력 검증 실패 (publicPath, pathPattern, upstreamUrl 규칙 위반) |
| `CDP-4003` | 403 | 소유자/역할 권한 부족 |
| `CDP-4009` | 409 | 상태 충돌 (예: application이 참조 중인 API 삭제 시도) |
| `CDP-4010` | 409 | Endpoint 중복 (요청 내부 dup / 기존 endpoint canonical 충돌 / DB UNIQUE 위반) |

CDP-4010 예:
```json
{
  "type": "https://cdp-portal.hd.com/errors/CDP-4010",
  "title": "Endpoint already exists",
  "status": 409,
  "code": "CDP-4010",
  "detail": "endpoints[1] duplicates endpoints[0]: GET /vendors/{vendorId} normalizes to '/vendors/{param}' (conflicts with '/vendors/{id}')",
  "instance": "/catalog/apis"
}
```

---

## 5. 등록/수정 파이프라인

### 5.1 Create (`POST /catalog/apis`)

```
Role check (api-owner | platform-admin)
  ↓
Public Base validation           (CDP-4002)
  ↓
Endpoint validation              (CDP-4002)
  ↓
요청 내부 중복 검증 (canonical)  (CDP-4010)
  ↓
DB transaction
  ├─ INSERT api_catalog (status=DRAFT)
  ├─ INSERT api_endpoint × N
  └─ UniqueViolationError catch → CDP-4010 (동시성 안전망)
  ↓
APISIX upsert_api_routes         (endpoint별 route + 플러그인 체인)
  ↓
smoke_test                       (401 기대)
  ↓
성공: UPDATE status=PUBLISHED   / 실패: DRAFT + warning
```

### 5.2 Patch (`PATCH /catalog/apis/{api_id}`)

```
소유권 확인
  ↓
Public Base validation (변경 시)
  ↓
Endpoint validation + 요청 내부 중복 검증 (endpoints 포함 시)
  ↓
DB: DELETE api_endpoint + INSERT (full replace) — UniqueViolation → CDP-4010
  ↓
APISIX route 재구성
  - Public Base 변경 시 이전 prefix delete_api_routes 먼저
  - upsert_api_routes + smoke_test
  - 실패 시 final_status = DRAFT + warning
  ↓
UPDATE api_catalog (변경 컬럼만)
  ↓
JWT 캐시 무효화 (Redis, 관련 PAT의 cdp:jwt:{token}:* 삭제)
```

Validation은 반드시 DELETE 이전에 수행되어, 검증 실패 시 기존 Endpoint가 보존된다(dedup spec §7).

### 5.3 Delete (`DELETE /catalog/apis/{api_id}`)

- application이 참조 중이면 CDP-4009 (RETIRED 상태 전환 안내)
- 참조 없으면 APISIX route 정리 → `DELETE FROM cdp.api_catalog` (CASCADE로 api_endpoint 자동 삭제)

---

## 6. APISIX Route 생성

### 6.1 Route 단위

**하나의 Endpoint = 하나의 APISIX Route**. Route ID:

```
capi-{apiCode-slug}-{version}-{method}-{endpointHash}
endpointHash = md5("{METHOD}|{final_public_path}")[:4]
```

동일 (method, final_public_path) 는 항상 동일 Route ID → 재PUT이 idempotent, 명칭 변경으로도 ID가 바뀌지 않는다.

레거시 API 단일 Route ID `capi-{slug}-{version}` 은 upsert 시 및 삭제 시에 정리된다(`_owns_route_id`).

### 6.2 Path 매칭 전략

APISIX 기본 radixtree는 `:name` 을 이해하지 않으므로 다음 두 형태를 조합한다(`_path_to_apisix_uri`, `apisix_client.py`).

- **파라미터가 없는 Endpoint**: `uri = final_public_path` (literal), priority 200.
- **파라미터가 있는 Endpoint**: 두 개의 URI 후보(`prefix`, `prefix + "/*"`) 로 매칭한 뒤, `vars` 에 anchored PCRE(`_path_to_regex_with_groups`)를 걸어 정확한 shape만 통과시킨다. priority 100.

이로써 `/vendors/summary`(literal, priority 200)가 `/vendors/{id}`(wildcard, priority 100)보다 우선하고, 파라미터 개수/위치가 다른 요청은 wildcard route에서도 걸러진다.

### 6.3 Path Parameter → Upstream 치환

- `_path_to_regex_with_groups`가 pattern을 anchored PCRE + 캡처 그룹 이름 리스트로 변환.
- `_upstream_path_replacement`가 upstream template의 `{name}` 를 `$1`, `$2` 로 대체.
- 최종적으로 `proxy-rewrite`의 `regex_uri = [source_regex, replaced_upstream_path]` 로 세팅.
- 파라미터가 없는 경우 `proxy-rewrite = {"uri": upstream_path}` (단순 rewrite).

### 6.4 플러그인 체인 (모든 Endpoint Route에 동일 세팅)

- `cors` — 프리플라이트 응답 (OPTIONS는 route methods에 항상 포함)
- `pat-auth` — PAT 검증 + `required_scope_map` 매치 (§10.1)
- `pat-quota` — 쿼터
- `pat-token-exchange` — PAT → 내부 JWT 교환 및 캐싱
- `proxy-rewrite` — URI 재작성 (§6.3)
- `pat-audit` — 감사 로그 스트림 XADD

`required_scope_map`은 Endpoint별 하나의 pair를 갖는다:
```lua
{
  [METHOD] = { { pattern = "^{final_public_path_regex}$", scope = "{required_scope}" } }
}
```

### 6.5 Route 관리 함수

- `upsert_api_routes(api, endpoints)` — 각 endpoint에 대해 PUT + 이 API에 속하지만 keep set에 없는 route는 삭제 (레거시 route ID 포함)
- `delete_api_routes(api_code, public_path)` — 이 API의 모든 route + 레거시 route 삭제
- `smoke_test(api, endpoints)` — 파라미터 없는 endpoint 하나를 credentials 없이 호출, `401` 응답 시 성공(pat-auth 체인이 살아있음을 확인)

### 6.6 Startup Resync

`services/portal-backend/main.py::resync_gateway_routes` 가 PUBLISHED 상태의 모든 API에 대해 `upsert_api_routes` 재실행. etcd가 비어있는 새 gateway 인스턴스에서 초기 상태 복구.

---

## 7. Frontend

### 7.1 화면 변경

- **API 등록** (`ApiRegisterForm.vue`) — 폼 필드: apiCode, name, description, ownerDept, publicPath, endpoints(Method + Pattern + Upstream Full URL + Required Scope + description). "Endpoint 추가" 버튼.
- **API 편집** (`ApiEditForm.vue`) — 로딩 시 `data.endpoints` → `form.endpoints` 매핑(`e.required_scope` → `requiredScope`). 저장 시 `endpoints` 배열 그대로 PATCH.
- **API 상세** (`ApiDetail.vue`) — "제공 Endpoint" 테이블: Method / Public Endpoint(최종 URL) / Upstream Full URL / Required Scope / 설명.
- **사용 신청** (`ApplicationForm.vue`) — `apiDetail.endpoints`에서 DISTINCT `required_scope`를 뽑아 체크박스로 표시. 각 항목에 해당 Scope가 적용되는 Endpoint 목록을 나열해 사용자가 최소 권한을 선택할 수 있게 한다.

### 7.2 등록/편집 화면의 클라이언트 사이드 중복 확인

- `normalizePattern()` 함수가 백엔드 `_normalize_endpoint_pattern` 과 동일 규칙 적용.
- Endpoint 배열 내 canonical key(`METHOD normalized-path`) 충돌을 실시간 감지, `duplicateWarning` 로 표시.
- 제출 버튼도 `duplicateWarning`이 있으면 차단.
- UI 결과는 최종 보장이 아님. 서버가 동일 검증을 재실행하며, 동시성은 DB UNIQUE로 방어.

### 7.3 오류 표시

- CDP-4002 / CDP-4010 이 반환되면 `detail` 필드를 그대로 노출한다.
- CDP-4010 검사가 서버에서 트리거된 경우, 응답의 `detail` 문구가 정확히 어떤 Endpoint끼리 충돌했는지 알려준다.

---

## 8. 감사(Audit) 및 관측성

Endpoint 리네임과 무관하게 유지:

- pat-audit 플러그인이 요청/응답 이벤트를 `cdp:audit:security` Redis Stream으로 XADD.
- portal-backend의 `audit_consumer` 가 이 Stream을 drain하여 `cdp.audit_log`(월 파티션)에 INSERT.
- `request_path`에는 최종 Public Path가 기록된다.

Prometheus 지표 이름 유지. Route 개수가 늘어남에 따라 APISIX 자체 지표(route count, config load)를 관측한다.

---

## 9. 검증(Validation) 및 예외 매트릭스

| 상황 | HTTP | 코드 | 검사 지점 |
|---|---|---|---|
| publicPath가 `/`로 시작하지 않음 | 400 | CDP-4002 | `_assert_valid_public_base` |
| publicPath에 `{` `:` `*` 포함 | 400 | CDP-4002 | 동상 |
| endpoints[i]의 필수 필드 누락 | 400 | CDP-4002 | `_assert_valid_endpoints` |
| pathPattern이 `/`로 시작하지 않음 | 400 | CDP-4002 | 동상 |
| pathPattern에 `?` `#` `*` 포함 | 400 | CDP-4002 | 동상 |
| pathPattern이 Public Base를 다시 포함 | 400 | CDP-4002 | 동상 |
| pathPattern에 `//` | 400 | CDP-4002 | `_normalize_endpoint_pattern` |
| upstreamUrl에 scheme/host 없음 | 400 | CDP-4002 | 동상 |
| upstreamUrl fragment 포함 | 400 | CDP-4002 | 동상 |
| upstream의 `{name}` 이 pattern에 미선언 | 400 | CDP-4002 | 동상 |
| 요청 내부 동일 canonical endpoint | 409 | CDP-4010 | `_assert_no_duplicate_endpoints` |
| DB UNIQUE 위반 (동시성) | 409 | CDP-4010 | asyncpg.UniqueViolationError catch |
| 존재하지 않는 api_id | 404 | NOT_FOUND | 각 라우터 |
| 소유자 아닌데 수정/삭제 | 403 | CDP-4003 | `_assert_can_modify` |
| Application이 참조 중인데 DELETE | 409 | CDP-4009 | `delete_api` |

---

## 10. 인가(Authorization) 흐름 (전체 컨텍스트)

이 절은 Endpoint의 `required_scope` 필드가 실제로 어떻게 강제되는지 재확인한다.

### 10.1 런타임 호출 흐름

```
Client ── Authorization: Bearer <PAT> ──▶ APISIX Route (per endpoint)

APISIX 체인:
  1. cors
  2. pat-auth (rewrite phase, prio 3010)
     - Redis/portal-backend에서 PAT 메타 조회
     - IP CIDR / 상태 검사
     - ctx.var.request_method 로 required_scope_map[method] 조회
     - ctx.var.uri 와 pattern regex 매치 → required_scope 확정
     - PAT.scopes 안에 required_scope 있는지 검사
       └─ 없으면 403 CDP-1003
     - ctx.cdp_scopes = granted_scopes 세팅
  3. pat-quota
  4. pat-token-exchange (access phase, prio 3000)
     - cache key: cdp:jwt:{tokenId}:{sha256(scopes)[:16]}
     - miss 시 TXS 호출 → { tokenId, userSub, scopes }
  5. proxy-rewrite (URI 재작성)
  6. pat-audit
  7. 실제 Upstream 호출
```

### 10.2 Token Exchange & Scope Claim

- TXS(`token-exchange-service`)가 Keycloak `token-exchange` grant에 `scope` 파라미터로 PAT.scopes를 그대로 전달.
- Keycloak IM에는 이 애플리케이션 Scope 값(예: `capi.vendor.read`)이 정의되어 있지 않지만, 요청된 scope 문자열이 JWT의 `scope` claim에 그대로 실려서 발행된다.
- Upstream은 이 JWT의 scope claim을 참조할 수 있다.
- **Scope 값 정합성(오타/미등록/철회 등)은 전적으로 Portal이 책임진다.** Endpoint 등록 시 값 자유 입력이므로, 시드/등록 워크플로에서 명명 규칙을 문서화하는 것이 좋다(예: `capi.<domain>.<read|write>`).

### 10.3 캐시 무효화

catalog PATCH가 라우팅에 영향 있는 변경(status / publicPath / endpoints)을 일으키면, 해당 API에 묶인 활성 PAT의 `cdp:jwt:{tokenId}:*` 를 Redis에서 삭제한다. PAT 자체는 유지되며, 다음 호출부터 새로운 scope 집합으로 Token Exchange가 재실행된다.

---

## 11. 시드 데이터

`infra/postgres/init/02_seed.sql` — 신 스키마 기준:

```sql
INSERT INTO cdp.api_endpoint (api_id, required_scope, http_method, path_pattern, upstream_url, description) VALUES
  ('a1000000-...-001', 'capi.vendor.read',  'GET',  '/',      'http://mock-internal-gw:8090/internal/api/v1/vendors', ...),
  ('a1000000-...-001', 'capi.vendor.read',  'GET',  '/{id}',  'http://mock-internal-gw:8090/internal/api/v1/vendors/{id}', ...),
  ('a1000000-...-001', 'capi.vendor.write', 'POST', '/',      'http://mock-internal-gw:8090/internal/api/v1/vendors', ...),
  ('a1000000-...-002', 'capi.order.read',   'GET',  '/',      ..., ...),
  ('a1000000-...-002', 'capi.order.read',   'GET',  '/{id}',  ..., ...),
  ('a1000000-...-003', 'capi.hr.read',      'GET',  '/',      ..., ...);
```

- MDM-VENDOR: read/write 분리를 통해 endpoint 단위 scope enforcement가 실제로 동작함을 시연.
- FIN-ORDER: 조회 전용 시나리오.
- HR-EMPLOYEE: 최소 범위 시나리오.

시드 application(`b2000000-...-001`)은 `granted_scopes = [capi.vendor.read]`만 부여 → 이 PAT은 POST /vendors 호출 시 403 CDP-1003 (endpoint-level scope 강제의 실증).

---

## 12. 테스트 매트릭스

### 12.1 자동 테스트 (integration/)

- `test_tc_e.py` (신설):
  - TC-E-01: `endpoints`/`requiredScope` payload 왕복
  - TC-E-02: trailing slash 차이 dup 감지 (CDP-4010)
  - TC-E-03: `{id}` vs `{vendorId}` canonical 충돌 (CDP-4010)
  - TC-E-04: 동일 pattern + 다른 method 공존 허용
  - TC-E-05: PATCH self-conflict 시 기존 endpoint 보존
  - TC-E-06: `/endpoints/check` API 동작
- `test_full_workflow.py`: 응답에 `endpoints` 존재 확인.
- 기존 `test_tc_a.py` / `test_tc_p.py` / `test_tc_q.py` / `test_tc_x.py` / `test_security.py`: PAT scope, quota, token exchange, security 시나리오 회귀 그대로 통과해야 함.

### 12.2 수동 검증 시나리오

- Public Base 변경 후 이전 route가 정리되는지 (`upsert_api_routes` pruning)
- APISIX startup resync가 PUBLISHED API를 모두 재PUT 하는지
- catalog PATCH 시 JWT 캐시 무효화 여부 (Redis `KEYS cdp:jwt:{tokenId}:*` 확인)
- application 참조가 있는 API DELETE → CDP-4009 안내
- OpenAPI 스펙 업로드 유지

---

## 13. 파일 변경 목록

| 파일 | 변경 |
|---|---|
| `services/portal-backend/error_handlers.py` | CDP-4002, CDP-4010 추가 |
| `services/portal-backend/routers/catalog.py` | endpoints DTO, `_normalize_endpoint_pattern`, `_endpoint_key`, `_assert_no_duplicate_endpoints`, `_endpoint_row_dicts`, `check_endpoint` GET 엔드포인트, UniqueViolation → CDP-4010, 응답 필드 rename |
| `services/portal-backend/apisix_client.py` | `endpoint["scope_name"]` → `endpoint["required_scope"]` |
| `services/portal-backend/routers/applications.py` | `SELECT DISTINCT required_scope FROM cdp.api_endpoint` |
| `services/portal-backend/main.py` | resync 쿼리와 endpoint dict 필드 rename |
| `services/portal-backend/db_migrations.py` | `_migrate_scope_table_to_endpoint` 추가, 기존 마이그레이션은 pre-rename shape 유지 |
| `infra/postgres/init/01_ddl.sql` | 신 스키마 정의 |
| `infra/postgres/init/02_seed.sql` | 신 컬럼명 INSERT |
| `frontend/portal-web/src/views/ApiRegisterForm.vue` | endpoints 배열 + 클라이언트 dup 감지 |
| `frontend/portal-web/src/views/ApiEditForm.vue` | 동상 + 응답 매핑 |
| `frontend/portal-web/src/views/ApiDetail.vue` | `apiDetail.endpoints`, `required_scope` 렌더 |
| `frontend/portal-web/src/views/ApplicationForm.vue` | DISTINCT scope 그룹 + endpoint 목록 표시 |
| `tests/integration/test_tc_e.py` | TC-E-01 ~ TC-E-06 (신설) |
| `tests/integration/test_full_workflow.py` | `endpoints` 존재 확인 |

`infra/postgres/migrations/2026_09_27_endpoint_upstream.sql` 은 legacy 참조본이며 그대로 유지.

---

## 14. 완료 기준 체크리스트

### 스펙 문서 A (Endpoint-Upstream 매핑) — SA-CHG-CDP-API-001
- [x] api_catalog.upstream_url 제거, api_endpoint.upstream_url 추가
- [x] Public Base + 상대 Pattern + Upstream Full URL 모델 반영
- [x] APISIX Route가 endpoint 단위 (route id 규칙 유지)
- [x] Path Parameter 치환 (regex_uri / uri rewrite)
- [x] 마이그레이션 (init + runtime backfill)

### 스펙 문서 B (Endpoint 명칭 전환)
- [x] `cdp.api_scope` → `cdp.api_endpoint`
- [x] `scope_id` → `endpoint_id`
- [x] `scope_name` → **`required_scope`** (스펙의 `endpoint_name` 대신 실질 시맨틱을 반영)
- [x] Request/Response: `scopes` → `endpoints`, `scopeName` → `requiredScope`
- [x] Backend 내부 함수/변수 endpoint 용어 통일
- [x] APISIX Client naming (route id 규칙 불변)
- [x] Frontend endpoint 용어
- [x] OAuth/PAT Scope 계열 용어 유지 (`application.requested_scopes`, `pat.scopes` 등)

### 스펙 문서 C (Endpoint 중복 및 Route 충돌 방지)
- [x] Create에서 INSERT 전 중복 검출 (요청 내부 + canonical)
- [x] PATCH에서 DELETE 전 검증
- [x] Path Parameter 이름 차이 및 trailing slash 차이 canonical 정규화
- [x] DB UNIQUE 유지 (동시성 안전망)
- [x] UniqueViolationError → CDP-4010 변환
- [x] 검증 실패 시 기존 PATCH endpoint 보존
- [x] 선택적 check API (`GET /catalog/apis/{api_id}/endpoints/check`)
- [x] Frontend 실시간 중복 확인
- [x] APISIX Route 알고리즘 미변경

### 통합 결정 사항
- [x] Endpoint 단위 required_scope 강제 유지 (write 계열 확장 대비)
- [x] `scope_name` → `endpoint_name` 대신 `required_scope`로 조정 (필드 시맨틱 명료화)
- [x] 문서, 코드, 테스트, 시드 일관성

---

## 15. 최종 원칙

> **API 는 Public Base 하나, Endpoint 는 (Method, 상대 Pattern, Upstream Full URL, Required Scope) 로 정의한다.**
>
> - Endpoint 중복은 canonical 정규화(파라미터 이름 무시, trailing slash 정리)로 사전 차단한다.
> - Endpoint 이름은 곧 (Method, Pattern) 이며 별도 표시 필드를 두지 않는다.
> - `required_scope` 는 이 Endpoint 를 호출하려면 PAT 에 있어야 하는 OAuth Scope 이며, 여러 Endpoint 가 공유할 수 있다.
> - OAuth/PAT Scope 용어(application.requested_scopes, pat.scopes 등) 는 변경 없이 유지한다.
> - DB UNIQUE 는 동시성에 대한 최종 안전망으로 유지한다.
> - Scope 값의 정합성은 Portal 이 단독 관리한다 (Keycloak IM 은 sign/mint 역할).

---

## 부록 A. 폐기된 대안 및 이유

- **`scope_name` → `endpoint_name`으로 직역 리네임** — 이 필드는 표시 이름이 아니라 필수 Scope 참조이므로 시맨틱 오해를 유발. `required_scope` 채택.
- **Endpoint 단위 Scope 완전 제거** — PAT ↔ API 는 1:1 이므로 API-level 만으로 충분하다는 의견 검토. Write 계열 API 노출 시 read/write 분리를 강제할 수단이 없어지므로 유지 결정.
- **Method 규약 기반 read/write (GET=read, 그 외=write)** — 단순화 이점은 있으나 `/summary`(GET/report-level) 같은 세분 시나리오 표현 불가. 규약보다 명시 필드 유지가 확장에 유리.

## 부록 B. 관련 문서

- `02_시민개발자_API포털_구현상세스펙.md` — 상위 구현 스펙 (SA-SPEC-CDP-002)
- `01_시민개발자_API포털_아키텍처정의서.md` — 아키텍처 정의서 (SA-ARCH-CDP-001)

세 개의 개별 원본 스펙(SA-CHG-CDP-API-001 Endpoint-Upstream 매핑 변경, Endpoint 명칭 전환, Endpoint 중복 및 Route 충돌 방지)은 본 문서로 완전히 통합되어 삭제되었다. 개별 변경 이력이 필요하면 `git log`를 참조한다.
