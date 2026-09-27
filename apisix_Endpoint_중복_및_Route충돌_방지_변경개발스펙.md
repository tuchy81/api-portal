# API Endpoint 중복 및 Route 충돌 방지 변경개발 스펙

- 대상 브랜치: `apisix`
- 대상 시스템: 시민개발자 API 포털 / Portal Backend / APISIX
- 기준 코드: `services/portal-backend/routers/catalog.py`, `services/portal-backend/apisix_client.py`, `infra/postgres/init/01_ddl.sql`
- 목적: API 등록 시 Endpoint 중복과 의미상 동일한 Path Parameter Route의 중복을 사전 차단하고, DB 제약을 최종 안전망으로 유지한다.

## 1. 현재 구현 분석

### 1.1 이미 존재하는 DB 안전망

`cdp.api_scope`에는 다음 제약이 있다.

```sql
UNIQUE (api_id, http_method, path_pattern)
```

따라서 동일 API의 동일 Method + 동일 원문 Pattern은 DB에서 차단된다.

또한 `create_api()`의 `api_catalog` INSERT와 `api_scope` INSERT는 하나의 transaction으로 묶여 있으므로 중복 발생 시 전체 등록이 rollback된다.

### 1.2 현재 문제

현재 `create_api()`와 `patch_api()`는 Endpoint 중복을 저장 전에 사전 검사하지 않는다.

따라서 사용자가 다음을 입력해도 입력 단계에서는 경고가 없다.

```
GET /vendors
GET /vendors
```

저장 시 DB UniqueViolation이 발생할 수 있으며, 이를 현재 코드가 업무 오류로 변환하지 않으므로 사용자에게 친절한 "이미 등록된 Endpoint" 응답을 제공하지 못한다.

### 1.3 더 중요한 Route 의미 중복

현재 DB Unique는 원문 문자열을 비교한다.

따라서 다음은 DB에서 서로 다른 값이다.

```
GET /vendors/{id}
GET /vendors/{vendorId}
GET /vendors/:id
```

그러나 APISIX의 Route 매칭 관점에서는 모두 같은 구조의 단일 Path Parameter Route다.

현재 `apisix_client.py`도 Path Parameter를 `([^/]+)` capture로 변환하므로, 변수명 차이는 실제 HTTP Route의 구별 요소가 아니다.

따라서 **Endpoint 중복 검증은 원문 문자열이 아니라 canonicalized Route Pattern 기준으로 수행해야 한다.**

## 2. 변경 목표

1. Create/PATCH 전에 Endpoint 중복을 검출한다.
2. 요청 배열 내부의 중복도 검출한다.
3. Path Parameter 이름만 다른 동일 Route Pattern도 검출한다.
4. trailing slash 등 동일 의미의 표현 차이를 정규화한다.
5. DB UNIQUE 제약은 유지한다.
6. 동시 등록에서 DB constraint가 발생해도 raw DB 오류를 노출하지 않고 HTTP 409로 변환한다.
7. 검증을 통과한 Endpoint만 APISIX Route 생성 단계로 전달한다.

## 3. Endpoint 중복 기준

최종 중복 Key는 다음이다.

```
API ID
+ HTTP Method
+ Normalized Path Pattern
```

예:

| 입력 | 정규화 | 결과 |
|---|---|---|
| GET /vendors | /vendors | 중복 |
| GET /vendors/ | /vendors | 중복 |
| GET /vendors/{id} | /vendors/{param} | 중복 |
| GET /vendors/{vendorId} | /vendors/{param} | 중복 |
| GET /vendors/:id | /vendors/{param} | 중복 |
| POST /vendors | /vendors | GET과 공존 가능 |
| GET /vendors/{id}/orders | /vendors/{param}/orders | 별도 Endpoint |

HTTP Method가 다르면 별도 Endpoint로 본다.

## 4. Path Pattern 정규화

Portal Backend에 다음 함수를 추가한다.

```python
_normalize_endpoint_pattern(path_pattern: str) -> str
```

정규화 규칙:

1. 반드시 `/`로 시작한다.
2. Root `/`을 제외한 마지막 slash는 제거한다.
3. 연속 slash는 validation error로 처리한다.
4. query string과 fragment는 허용하지 않는다.
5. `{name}`과 `:name` 형식의 Path Parameter를 동일한 canonical token `{param}`으로 변환한다.
6. Parameter의 개수와 위치는 유지한다.
7. Public Base가 다시 포함된 Pattern은 기존 validation처럼 거부한다.
8. wildcard `*`는 현재 모델에서 허용하지 않는다.

예:

```
/vendors/
/vendors
    → /vendors

/vendors/{id}
/vendors/{vendorId}
/vendors/:id
    → /vendors/{param}

/vendors/{id}/orders
    → /vendors/{param}/orders
```

## 5. Create API 변경

대상:

```
services/portal-backend/routers/catalog.py
```

현재:

```
validate
→ INSERT api_catalog/api_scope
→ APISIX
```

변경:

```
Public Base validation
→ Endpoint validation
→ 요청 Scope 내부 중복 검증
→ 기존 DB Endpoint와 canonical conflict 검증
→ DB transaction
→ APISIX Route upsert
→ Smoke Test
→ PUBLISHED
```

중복이면 DB INSERT 전에 종료한다.

### 오류 응답

HTTP Status:

```
409 Conflict
```

권장 코드:

```
CDP-4010
```

예:

```json
{
  "code": "CDP-4010",
  "message": "Endpoint already exists",
  "details": {
    "method": "GET",
    "pathPattern": "/vendors/{vendorId}",
    "normalizedPattern": "/vendors/{param}",
    "conflictWith": "/vendors/{id}"
  }
}
```

## 6. 요청 내부 중복 검증

DB 조회만으로는 부족하다. 아직 DB에 저장되지 않은 같은 요청 내부의 중복도 먼저 검사한다.

예:

```
scopes:
  GET /vendors
  GET /vendors
```

또는:

```
GET /vendors/{id}
GET /vendors/{vendorId}
```

둘 모두 하나의 요청 안에서 충돌로 처리한다.

권장 구현:

```python
seen = {}

for scope in scopes:
    key = (
        scope["httpMethod"].upper(),
        _normalize_endpoint_pattern(scope["pathPattern"])
    )
    if key in seen:
        # CDP-4010 / 409
```

## 7. 기존 DB Endpoint와 충돌 검증

기존 API를 PATCH하는 경우 새 Scope 전체 집합을 기준으로 검사한다.

권장 흐름:

```
새 scopes 수신
    ↓
전체 validation
    ↓
canonical key 생성
    ↓
입력 내부 중복 검사
    ↓
기존 api_scope 조회
    ↓
canonical key 비교
    ↓
충돌이면 409
```

PATCH의 현재 구현은 Scope 전체를 DELETE 후 INSERT하는 방식이므로 **검증을 DELETE보다 반드시 먼저 수행한다.**

검증 실패 시 기존 Scope를 변경하지 않는다.

## 8. 자기 자신과의 충돌 처리

PATCH에서 기존 Endpoint와 새 Endpoint가 같은 것은 정상이다.

따라서 기존 DB 데이터와 비교할 때는 동일 API의 기존 Scope를 무조건 오류로 처리하지 말고, **새 Scope 집합 자체의 중복 여부를 기준으로 판단한다.**

즉:

```
기존:
GET /vendors

PATCH:
GET /vendors
GET /vendors/{id}
```

→ 정상.

반면:

```
PATCH:
GET /vendors/{id}
GET /vendors/{vendorId}
```

→ 409.

## 9. DB UNIQUE 제약 유지

현재 제약은 그대로 유지한다.

```sql
UNIQUE (api_id, http_method, path_pattern)
```

애플리케이션 사전 검증은 UX와 canonical conflict 검출을 위한 것이며, DB constraint는 동시성에 대한 최종 안전망이다.

예:

```
요청 A ── 사전검증 OK ──┐
                        ├── DB INSERT
요청 B ── 사전검증 OK ──┘
```

동시에 들어오면 둘 다 사전검증을 통과할 수 있으므로 DB UNIQUE가 필요하다.

## 10. UniqueViolation 예외 처리

Create/PATCH 저장 중 `asyncpg.UniqueViolationError`가 발생할 경우 raw DB 오류를 사용자에게 노출하지 않는다.

처리:

```
asyncpg.UniqueViolationError
        ↓
Endpoint UNIQUE 충돌 여부 확인
        ↓
CDP-4010 / HTTP 409
```

다른 DB 오류는 기존 오류 처리 흐름을 유지한다.

## 11. APISIX Route 충돌 방지

현재 Route ID는:

```
MD5(HTTP Method + final_public_path)[:4]
```

기준으로 생성된다.

따라서 다음과 같은 의미상 동일 Endpoint를 사전에 차단해야 한다.

```
GET /capi/v1/vendors/{id}
GET /capi/v1/vendors/{vendorId}
```

두 Endpoint는 parameter 이름을 제외하면 동일한 Route 구조이므로 둘을 동시에 APISIX에 등록하지 않는다.

이번 변경에서는 Route ID 알고리즘을 변경하지 않는다. **등록 단계에서 canonical conflict를 차단하는 것이 우선이다.**

## 12. Prefix/Wildcard Endpoint와의 구분

이번 변경은 현재 코드가 지원하는 concrete pattern을 대상으로 한다.

지원:

```
/vendors
/vendors/{id}
/vendors/{id}/orders
```

지원하지 않음:

```
/vendors/*
/vendors/**
```

Prefix wildcard가 향후 필요하면 별도의 `matchType` 또는 명시적인 wildcard 정책으로 설계해야 하며, 이번 중복 검증 범위에 섞지 않는다.

## 13. UI 입력 단계 중복 확인

Backend의 최종 검증과 별개로 Frontend에서 사용자가 Endpoint를 입력하는 시점에 중복 확인을 제공한다.

권장 UX:

```
Method + Pattern 입력
        ↓
debounce / blur
        ↓
중복 확인
        ↓
✓ 사용 가능
또는
⚠ 이미 등록된 Endpoint
```

단, UI 결과는 최종 보장이 아니다.

최종 저장 API가 반드시 동일한 canonical validation을 수행한다.

### 선택적 Check API

```
GET /catalog/apis/{api_id}/endpoints/check
  ?httpMethod=GET
  &pathPattern=/vendors/{id}
```

응답 예:

```json
{
  "available": false,
  "normalizedPattern": "/vendors/{param}",
  "conflict": {
    "scopeName": "vendor.read",
    "httpMethod": "GET",
    "pathPattern": "/vendors/{vendorId}"
  }
}
```

별도 Check API를 구현하지 않아도 Create/PATCH의 서버 검증은 필수다.

## 14. 테스트 요구사항

### 14.1 정상

- GET /vendors
- POST /vendors
- GET /vendors/{id}
- GET /vendors/{id}/orders
- GET /vendors + POST /vendors

### 14.2 중복

- GET /vendors + GET /vendors
- GET /vendors/ + GET /vendors
- GET /vendors/{id} + GET /vendors/{vendorId}
- GET /vendors/:id + GET /vendors/{id}
- 동일 API의 동일 Method + 동일 normalized Pattern

### 14.3 허용

- GET /vendors + POST /vendors
- GET /vendors + GET /vendors/{id}
- GET /vendors/{id} + GET /vendors/{id}/orders

### 14.4 동시성

- 동일 Endpoint 동시 등록
- 사전검증 동시 통과 후 DB UNIQUE 충돌
- UNIQUE violation → 409 변환
- DB transaction rollback 확인

### 14.5 PATCH

- 기존 Endpoint 유지하면서 다른 Endpoint 추가
- 기존 Endpoint와 동일 Endpoint 입력
- parameter 이름만 변경
- 중복 Scope를 포함한 전체 Scope 교체
- 검증 실패 시 기존 Scope 유지

### 14.6 APISIX

- 동일 normalized Route가 APISIX에 중복 생성되지 않는지 확인
- /vendors/{id}와 /vendors/{vendorId}가 동시에 존재하지 않는지 확인
- 정상 Endpoint의 기존 Route ID가 불필요하게 변경되지 않는지 확인

## 15. 완료 기준

- [ ] Create에서 Endpoint 중복을 INSERT 전에 검출
- [ ] PATCH에서 Scope 전체 교체 전에 검출
- [ ] 요청 배열 내부 중복 검출
- [ ] Path Parameter 이름 차이 검출
- [ ] trailing slash 차이 검출
- [ ] DB UNIQUE 유지
- [ ] UniqueViolation을 409 업무 오류로 변환
- [ ] 중복 Endpoint가 APISIX로 전달되지 않음
- [ ] 검증 실패 시 기존 PATCH Scope 유지
- [ ] 동시성 테스트 통과
- [ ] 기존 정상 Endpoint 등록/수정 회귀 테스트 통과

## 16. 개발 대상 파일

| 파일 | 변경 |
|---|---|
| `services/portal-backend/routers/catalog.py` | canonicalization, 중복 검증, 409 예외 처리 |
| `services/portal-backend/apisix_client.py` | Route 충돌 방지를 위한 canonical semantics와의 정합성 검증 |
| `infra/postgres/init/01_ddl.sql` | 기존 UNIQUE 유지, 변경 불필요 |
| Frontend API 등록 화면 | 입력 단계 Endpoint 중복 확인/표시 |
| 관련 테스트 | Create/PATCH/정규화/동시성/APISIX 회귀 |

## 17. 구현 원칙

> **입력 단계에서는 즉시 알려주고, 서버에서는 canonical 기준으로 다시 검증하며, DB UNIQUE는 최종 안전망으로 유지한다.**

최종 흐름:

```
Endpoint 입력
    ↓
[선택] 실시간 중복 확인
    ↓
Create/PATCH
    ↓
Canonical Validation
    ↓
내부 중복 검사
    ↓
DB 기존 데이터와 충돌 검사
    ↓
DB Transaction
    ↓
DB UNIQUE 최종 방어
    ↓
APISIX Route Reconciliation
    ↓
Smoke Test
```
