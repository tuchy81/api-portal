# 시민개발자 API 포털 API 등록 구조 변경 개발 스펙

문서 ID: SA-CHG-CDP-API-001  
대상 브랜치: apisix  
변경 유형: API 등록 모델 및 APISIX Endpoint 라우팅 구조 변경  
작성일: 2026-09-26

## 1. 변경 목적

현재 API 등록은 API 단위의 publicPath와 upstreamUrl을 사용한다. 이 구조에서는 API별 공통 upstream base와 Method별 실제 Backend 주소의 관계를 별도로 해석해야 한다.

변경 후에는 다음 원칙을 적용한다.

> API에는 Public Base 하나만 정의하고, Endpoint는 Public Base에 붙는 상대 Pattern으로 정의하며, Endpoint별 Upstream은 실제 호출할 Full URL을 직접 등록한다.

개념 모델:

    API
     └─ Public Base
          /capi/v1/vendors
              ├─ GET /       → http://vendor-service:8080/internal/api/v1/vendors
              ├─ GET /{id}   → http://vendor-service:8080/internal/api/v1/vendors/{id}
              ├─ POST /      → http://vendor-command:8090/api/vendor/create
              └─ GET /summary → http://vendor-query:8080/query/vendor-summary

핵심 효과:
- 시민개발자는 Public API 주소 체계를 직관적으로 정의한다.
- Backend Resource Path는 Public API와 달라도 된다.
- 하나의 Public API가 여러 Backend Service로 매핑될 수 있다.
- Gateway가 암묵적인 Path 규칙을 추론하지 않고 명시적인 Endpoint 매핑을 사용한다.

## 2. 변경 후 개념 모델

### 2.1 API

API는 시민개발자에게 공개되는 하나의 논리적 API 묶음이다.

| 항목 | 의미 | 예 |
|---|---|---|
| API Code | API 식별자 | MDM-VENDOR |
| Name | API 이름 | 공급사 API |
| Public Base | 공개 기준 주소 | /capi/v1/vendors |
| Owner | 운영 부서/담당자 | MDM팀 |
| Status | DRAFT/PUBLISHED 등 | PUBLISHED |

API에는 Upstream URL을 두지 않는다.

### 2.2 Endpoint

Endpoint는 실제 제공 기능 하나를 의미한다.

| 항목 | 의미 | 예 |
|---|---|---|
| HTTP Method | 요청 방식 | GET |
| Pattern | Public Base에 붙는 상대 경로 | /{id} |
| Upstream URL | 실제 Backend 호출 주소 전체 | http://vendor-service:8080/internal/api/v1/vendors/{id} |
| Scope | 호출 권한 | capi.vendor.read |
| Description | 기능 설명 | 공급사 단건 조회 |

## 3. 등록 규칙

### 3.1 Public Base

Public Base는 API의 기준 주소다.

예:
    /capi/v1/vendors

규칙:
1. 반드시 /로 시작한다.
2. API 버전은 Public Base에 포함한다.
3. Path Parameter를 포함하지 않는다.
4. Endpoint 상세 기능 경로를 포함하지 않는다.
5. API 전체에서 유일해야 한다.

허용:
    /capi/v1/vendors
    /capi/v1/orders
    /capi/v2/vendors

금지:
    /capi/v1/vendors/{id}
    /capi/v1/vendors/:id
    /capi/v1/vendors/search

### 3.2 Endpoint Pattern

Pattern은 Public Base에 붙이는 상대 경로다.

허용:
    /
    /{id}
    /search
    /{id}/orders

최종 Public Endpoint는 다음으로 계산한다.

    Public Endpoint = Public Base + Pattern

예:
    Public Base = /capi/v1/vendors
    Pattern     = /{id}
    결과        = /capi/v1/vendors/{id}

Pattern이 /인 경우 최종 주소는 Public Base 자체가 된다.

### 3.3 Upstream URL

Upstream은 Endpoint별 실제 호출 Full URL이다.

예:
    http://vendor-service:8080/internal/api/v1/vendors
    http://vendor-service:8080/internal/api/v1/vendors/{id}
    http://vendor-query:8080/query/vendor-summary

규칙:
1. scheme을 포함한다.
2. Host/Port를 포함한다.
3. Backend의 실제 Resource Path까지 포함한다.
4. Public Base와 동일한 Path 구조일 필요가 없다.
5. Endpoint Pattern의 Path Parameter는 동일한 이름으로 표시한다.
6. Public API와 Backend API 구조가 달라도 허용한다.

예:

| Public Endpoint | Upstream |
|---|---|
| /capi/v1/orders | http://order:8080/api/orders |
| /capi/v1/orders/{id} | http://order:8080/api/order/{id} |
| /capi/v1/orders/summary | http://order-query:8080/query/order-summary |

## 4. 최종 등록 예시

API 기본 정보:

    API Code: MDM-VENDOR
    Name: 공급사 API
    Public Base: /capi/v1/vendors

Endpoint:

| Method | Pattern | 최종 Public Path | Upstream Full URL | Scope |
|---|---|---|---|---|
| GET | / | /capi/v1/vendors | http://vendor-service:8080/internal/api/v1/vendors | capi.vendor.read |
| GET | /{id} | /capi/v1/vendors/{id} | http://vendor-service:8080/internal/api/v1/vendors/{id} | capi.vendor.read |
| POST | / | /capi/v1/vendors | http://vendor-command:8090/api/vendor/create | capi.vendor.write |
| GET | /summary | /capi/v1/vendors/summary | http://vendor-query:8080/query/vendor-summary | capi.vendor.read |

POST의 Public Path와 Upstream Path가 달라도 허용한다. GET /summary가 별도 Backend Service로 매핑되는 것도 허용한다.

## 5. 데이터 모델 변경

### 5.1 기존

    cdp.api_catalog
      public_path
      upstream_url       ← API 공통

    cdp.api_scope
      http_method
      path_pattern

### 5.2 변경

    cdp.api_catalog
      public_path         ← Public Base

    cdp.api_scope
      http_method
      path_pattern        ← Public Base에 붙는 상대 Pattern
      upstream_url        ← Endpoint별 Full URL

### 5.3 DDL 변경

    ALTER TABLE cdp.api_scope
        ADD COLUMN upstream_url VARCHAR(500);

    -- 신규 코드 전환 및 기존 데이터 마이그레이션 완료 후
    ALTER TABLE cdp.api_catalog
        DROP COLUMN upstream_url;

실제 운영 마이그레이션은 다음 순서를 따른다.

1. api_scope.upstream_url 추가
2. 기존 API 데이터 변환
3. 신규 코드에서 Endpoint upstream 사용
4. 기존 API 전체 라우팅 검증
5. api_catalog.upstream_url 참조 코드 제거
6. 최종 컬럼 삭제

## 6. Backend API Contract 변경

### 6.1 POST /catalog/apis

기존의 API-level upstreamUrl은 제거한다.

변경 요청 예:

    {
      "apiCode": "MDM-VENDOR",
      "name": "공급사 API",
      "publicPath": "/capi/v1/vendors",
      "scopes": [
        {
          "scopeName": "capi.vendor.read",
          "httpMethod": "GET",
          "pathPattern": "/",
          "upstreamUrl": "http://vendor-service:8080/internal/api/v1/vendors",
          "description": "공급사 목록 조회"
        },
        {
          "scopeName": "capi.vendor.read",
          "httpMethod": "GET",
          "pathPattern": "/{id}",
          "upstreamUrl": "http://vendor-service:8080/internal/api/v1/vendors/{id}",
          "description": "공급사 단건 조회"
        }
      ]
    }

### 6.2 필드 변경

| 기존 | 변경 |
|---|---|
| api_catalog.upstream_url | 제거 |
| ApiCreateRequest.upstreamUrl | 제거 |
| ApiPatchRequest.upstreamUrl | 제거 |
| scope.pathPattern | 상대 Pattern으로 변경 |
| scope.upstreamUrl | 신규 |
| publicPath | Public Base 의미로 고정 |

## 7. 입력 검증

### 7.1 Public Base

Path Parameter를 포함하면 400을 반환한다.

예:
    /capi/v1/vendors/{id}
    /capi/v1/vendors/:id

메시지:
    Public Base에는 Path Parameter를 사용할 수 없습니다.
    Endpoint Pattern에 정의하십시오. 예: /{id}

### 7.2 Pattern

Pattern은 반드시 상대 경로여야 한다.

허용:
    /
    /{id}
    /search
    /{id}/orders

거부:
    capi/v1/vendors
    /capi/v1/vendors/{id}
    /capi/v1/vendors/search

Public Base를 다시 포함하면 등록 오류로 처리한다.

### 7.3 Upstream URL

검증 항목:
- 절대 URL 필수
- scheme 필수
- host 필수
- fragment 금지
- Pattern과 Upstream Path Parameter 구조 검증
- 운영 환경에서는 허용된 내부 Host/Network 정책 적용

예:
    Pattern: /{id}
    Upstream: http://vendor-service:8080/vendors/{id}

정상.

다음은 명시적 오류로 처리한다.

    Pattern: /{id}
    Upstream: http://vendor-service:8080/vendors

Path Parameter가 사라지므로 자동 허용하지 않는다.

## 8. APISIX Route 생성 변경

현재 구현은 API 단위로 하나의 Upstream을 생성한다.

변경 후에는 Endpoint별 Route를 생성한다.

예:

    GET /capi/v1/vendors
      → Route: capi-mdm-vendor-v1-get-root
      → http://vendor-service:8080/internal/api/v1/vendors

    GET /capi/v1/vendors/123
      → Route: capi-mdm-vendor-v1-get-id
      → http://vendor-service:8080/internal/api/v1/vendors/123

    GET /capi/v1/vendors/summary
      → Route: capi-mdm-vendor-v1-get-summary
      → http://vendor-query:8080/query/vendor-summary

각 Endpoint Route에는 다음을 반영한다.
- URI 또는 URI Pattern
- HTTP Method
- Endpoint별 Upstream
- PAT Auth
- PAT Quota
- Token Exchange
- Audit
- CORS
- 필요한 Path Rewrite

### 8.1 Path 계산

    finalPublicPath = normalize(publicBase + pattern)

예:
    publicBase = /capi/v1/vendors
    pattern    = /{id}
    result     = /capi/v1/vendors/{id}

### 8.2 Path Parameter 치환

Pattern:
    /{id}

실제 요청:
    /capi/v1/vendors/123

Upstream Template:
    http://vendor-service:8080/internal/api/v1/vendors/{id}

최종 호출:
    http://vendor-service:8080/internal/api/v1/vendors/123

구현 시 Public Request의 Path Parameter를 추출하고 Upstream Template의 동일 Parameter 위치에 치환한다.

## 9. Scope 처리

Scope는 Endpoint 접근 권한을 나타낸다.

    Endpoint
      ├─ Method
      ├─ Pattern
      ├─ Upstream
      └─ Scope

PAT에 capi.vendor.read가 있으면 해당 Scope에 연결된 Endpoint를 호출할 수 있다.

PAT Auth 내부 비교용 Pattern은 최종 Public Path로 정규화한다.

    Public Base: /capi/v1/vendors
    Pattern: /{id}

    Authorization Pattern:
    /capi/v1/vendors/{id}

즉, 외부 등록 모델과 내부 권한 검증 모델을 분리한다.

## 10. Route ID 규칙

Endpoint Route가 여러 개이므로 API Code만으로 Route ID를 만들면 충돌한다.

권장 형식:

    capi-{apiCode}-{version}-{method}-{endpointHash}

예:
    capi-mdm-vendor-v1-get-a81f
    capi-mdm-vendor-v1-get-b72c
    capi-mdm-vendor-v1-post-a81f

Endpoint Hash 입력값:

    HTTP Method + normalized Public Path

예:
    GET|/capi/v1/vendors/{id}

Route ID는 Method와 Public Path가 동일하면 동일하게 생성되어야 하며, 불필요하게 매번 변경되지 않아야 한다.

## 11. API 수정(PATCH)

다음 변경은 APISIX Route 재구성이 필요하다.

- Public Base 변경
- Endpoint 추가
- Endpoint 삭제
- Method 변경
- Pattern 변경
- Upstream 변경
- Scope 변경
- API Status 변경

처리 순서:

    PATCH
      → DB 입력값 검증
      → APISIX 기존 Endpoint Route 정리
      → 신규 Endpoint Route 생성
      → Smoke Test
      → 성공: PUBLISHED
      → 실패: DRAFT

기존 구현의 "Gateway 등록/검증 성공 후 PUBLISHED" 원칙을 유지한다.

## 12. 서로 다른 Backend 구조 허용

다음 API는 하나의 Public API로 등록할 수 있어야 한다.

    Public Base: /capi/v1/orders

| Method | Pattern | Upstream |
|---|---|---|
| GET | / | http://order-query:8080/api/orders |
| GET | /{id} | http://order-query:8080/api/order/{id} |
| POST | / | http://order-command:8090/order/create |
| POST | /{id}/cancel | http://order-command:8090/order/{id}/cancel |
| GET | /summary | http://order-report:8080/report/order-summary |

시민개발자가 보는 API와 Backend Resource 구조를 분리한다.

## 13. OpenAPI 연계

OpenAPI가 제공되면 Endpoint 등록 정보와 비교 검증한다.

예:

    OpenAPI Paths
      /vendors
      /vendors/{id}
      /vendors/summary

    Public Base
      /capi/v1/vendors

    Portal Pattern
      /
      /{id}
      /summary

OpenAPI Path와 Portal Pattern을 정규화하여 불일치를 검출한다.

단, OpenAPI의 servers.url을 Endpoint Upstream URL로 자동 사용하지 않는다. Endpoint Upstream은 Gateway가 실제 호출해야 하는 내부 주소를 명시적으로 등록하는 값이다.

## 14. Frontend 변경

API 등록 화면은 다음 구조로 변경한다.

    API 기본 정보
    --------------------------------
    API Code       [ MDM-VENDOR ]
    API 이름       [ 공급사 API ]
    설명           [ ... ]

    API 공개 주소
    [ /capi/v1/vendors ]

    제공 API
    ----------------------------------------------------------------
    Method   Pattern       Upstream Full URL                    Scope
    GET      /             http://vendor:8080/api/vendors      read
    GET      /{id}         http://vendor:8080/api/vendors/{id} read
    POST     /             http://command:8090/vendor/create   write

화면에서 최종 Public URL도 함께 표시한다.

    GET /capi/v1/vendors/{id}
    → http://vendor-service:8080/internal/api/v1/vendors/{id}

사용자가 Pattern이 Public Base에 붙는 구조를 즉시 이해할 수 있어야 한다.

## 15. 데이터 마이그레이션

기존 데이터:

    publicPath  = /capi/v1/vendors
    upstreamUrl = http://vendor-service:8080/internal/api/v1

    GET /capi/v1/vendors
    GET /capi/v1/vendors/{id}

변경 데이터:

    publicPath = /capi/v1/vendors

    GET /
      → http://vendor-service:8080/internal/api/v1/vendors

    GET /{id}
      → http://vendor-service:8080/internal/api/v1/vendors/{id}

기존 API의 upstream resource name은 현재 Gateway rewrite 규칙을 기준으로 계산한다.

마이그레이션 검증:
- 기존 Public URL 호출 결과 동일
- Method 동일
- Path Parameter 정상 전달
- Scope 인증 결과 동일
- PAT Quota 정상 동작
- Token Exchange 정상 수행
- Audit Log request_path 정상 기록

## 16. 테스트 요구사항

### 등록
- [ ] Public Base 등록 성공
- [ ] Public Base에 Path Parameter 입력 시 400
- [ ] Pattern / 등록 성공
- [ ] Pattern /{id} 등록 성공
- [ ] Pattern에 Public Base를 다시 입력하면 400
- [ ] Upstream Full URL 등록 성공
- [ ] 상대 Upstream URL 거부
- [ ] Pattern/Upstream Parameter 불일치 검출

### Routing
- [ ] GET root → 지정 Upstream
- [ ] GET parameter → Parameter 치환 후 지정 Upstream
- [ ] POST root → 다른 Upstream 가능
- [ ] GET summary → 별도 Service Upstream 가능
- [ ] 동일 Method의 여러 Pattern 정상 분기
- [ ] 서로 다른 Method의 동일 Pattern 정상 분기

### Security
- [ ] Endpoint별 Scope 검증
- [ ] Scope 없는 호출 403
- [ ] PAT 인증 실패 401
- [ ] Token Exchange 정상 수행
- [ ] 신뢰 Header만 Upstream 전달
- [ ] Audit Log에 최종 Public Path 기록

### Lifecycle
- [ ] Endpoint 추가 시 Route 추가
- [ ] Endpoint 삭제 시 Route 삭제
- [ ] Upstream 변경 시 Route 갱신
- [ ] Public Base 변경 시 기존 Route 정리
- [ ] Publish 실패 시 DRAFT 유지
- [ ] 수정 후 JWT Cache 무효화 기존 정책 유지

## 17. 개발 대상 파일

| 파일 | 주요 변경 |
|---|---|
| services/portal-backend/routers/catalog.py | Request Model, CRUD, Endpoint Upstream 처리 |
| services/portal-backend/apisix_client.py | Endpoint별 Route 생성 및 Upstream 매핑 |
| infra/postgres/init/01_ddl.sql | api_scope.upstream_url 추가, api_catalog.upstream_url 제거 |
| Frontend API 등록 화면 | Public Base + Endpoint Pattern + Upstream Full URL |
| 관련 테스트 | 등록/검증/라우팅/마이그레이션 |

## 18. 최종 개발 원칙

> API에는 Public Base 하나만 두고, 각 Endpoint는 상대 Pattern으로 정의하며, Endpoint가 실제 호출할 Upstream Full URL을 직접 매핑한다.

최종 모델:

    API
    │
    ├── Public Base
    │     /capi/v1/vendors
    │
    └── Endpoint[]
          ├── Method
          ├── Pattern
          ├── Upstream Full URL
          ├── Scope
          └── Description

이 구조를 통해 Public API 모델과 Backend 구현 경로를 완전히 분리하고, 하나의 Public API가 여러 Backend Service/Resource로 자연스럽게 조합되도록 한다.
