# Endpoint 명칭 전환 개발 스펙

## 1. 목적

현재 cdp.api_scope 및 API DTO의 scopeName / scopes는 실제 기능상 API Endpoint를 의미한다. 이를 Endpoint라는 명확한 도메인 용어로 전환한다.

목표 모델:

API Catalog
- Public Base
- Endpoints
  - endpointName
  - HTTP Method
  - Path Pattern
  - Upstream URL

핵심 원칙은 실제 Endpoint를 의미하는 Scope 명칭만 Endpoint로 변경하고, OAuth/PAT 권한 범위를 의미하는 진짜 Scope는 변경하지 않는 것이다.

## 2. 용어 정의

| 현재 용어 | 변경 용어 | 의미 |
|---|---|---|
| API Scope | API Endpoint | Public API의 실제 호출 단위 |
| scopeName | endpointName | Endpoint 식별/표시 이름 |
| scopes (API 등록 요청) | endpoints | API에 포함되는 Endpoint 목록 |
| api_scope | api_endpoint | Endpoint 저장 테이블 |
| scope_id | endpoint_id | Endpoint PK |
| scope_name | endpoint_name | Endpoint 이름 |

다음은 OAuth/PAT 권한 범위이므로 Scope라는 용어를 그대로 유지한다.

- application.requested_scopes
- application.granted_scopes
- pat.scopes
- vendor.read / vendor.write 같은 OAuth Scope

최종 개념:

Endpoint = 실제 HTTP 호출 단위
Scope = 호출 권한의 범위

## 3. 목표 모델

예:

Public Base: /capi/v1/vendors

- GET / -> http://vendor-service:8080/internal/api/v1/vendors
- GET /{id} -> http://vendor-service:8080/internal/api/v1/vendors/{id}
- POST / -> http://vendor-command:8090/api/vendor/create
- GET /summary -> http://vendor-query:8080/query/vendor-summary

최종 Public URL은 Public Base + Endpoint Path Pattern으로 구성한다.

## 4. Database 변경

현재 cdp.api_scope를 cdp.api_endpoint로 변경한다.

컬럼 변경:

- scope_id -> endpoint_id
- scope_name -> endpoint_name

다음 컬럼은 유지한다:

- api_id
- http_method
- path_pattern
- upstream_url
- description

최종 테이블 개념:

cdp.api_endpoint(
  endpoint_id,
  api_id,
  endpoint_name,
  http_method,
  path_pattern,
  upstream_url,
  description
)

UNIQUE(api_id, http_method, path_pattern)은 유지한다. Endpoint 중복 방지 스펙에서 정의한 canonicalization 검증도 유지한다.

운영 DB에서는 초기 DDL만 수정하지 말고 migration을 수행한다.

권장 순서:
1. cdp.api_scope -> cdp.api_endpoint
2. scope_id -> endpoint_id
3. scope_name -> endpoint_name
4. PK/FK/UNIQUE/INDEX/Constraint 이름 확인 및 정리
5. 기존 데이터 검증
6. 애플리케이션 전환
7. 기존 api_scope 참조 제거

## 5. Backend API 변경

Create/Patch 요청에서 API Endpoint 목록은 scopes 대신 endpoints를 사용한다.

예:

{
  "apiCode": "VENDOR",
  "publicPath": "/capi/v1/vendors",
  "endpoints": [
    {
      "endpointName": "Vendor Detail",
      "httpMethod": "GET",
      "pathPattern": "/{id}",
      "upstreamUrl": "http://vendor:8080/api/vendors/{id}"
    }
  ]
}

조회 Response도 scopes 대신 endpoints를 사용한다.

기존 외부 소비자가 존재하는 경우 호환 기간을 별도로 정의한다. 신규 표준 계약은 endpoints를 사용하고, 내부 표준 모델은 즉시 Endpoint로 통일한다.

## 6. Backend 내부 변경

주요 명칭을 다음과 같이 변경한다.

| 현재 | 변경 |
|---|---|
| _assert_valid_scopes | _assert_valid_endpoints |
| _scope_row_dicts | _endpoint_row_dicts |
| scope | endpoint |
| scopes | endpoints |
| scope_name | endpoint_name |
| scopeName | endpointName |
| api_scope | api_endpoint |

예:

def _assert_valid_endpoints(public_path, endpoints, path_for_error):
    ...

내부 모델에서도 endpoint_name, http_method, path_pattern, upstream_url을 사용한다.

## 7. APISIX Client 변경

APISIX는 이미 실제로 Endpoint 단위로 Route를 생성하고 있으므로 명칭만 명확하게 맞춘다.

주요 함수:

- build_endpoint_route(api, endpoint)
- upsert_api_routes(api, endpoints)

Route ID 생성 규칙은 변경하지 않는다.

capi-{apiCode}-{version}-{method}-{endpointHash}

Endpoint Hash 기준도 기존과 동일하게 유지한다.

MD5("METHOD|final_public_path")[:4]

따라서 이번 명칭 변경으로 APISIX Route ID가 불필요하게 변경되어서는 안 된다.

## 8. Endpoint 중복 검증

이전 Endpoint 중복 방지 스펙을 그대로 적용한다.

논리적 식별자는:

API + HTTP Method + Normalized Path Pattern

예:

- GET /vendors 와 GET /vendors/ -> 동일 Endpoint
- GET /vendors/{id}, GET /vendors/{vendorId}, GET /vendors/:id -> 동일 Endpoint
- GET /vendors 와 POST /vendors -> 서로 다른 Endpoint

이번 작업은 명칭 전환이며, canonicalization 및 중복 방지 정책 자체는 변경하지 않는다.

## 9. Endpoint와 OAuth/PAT Scope 분리

두 개념을 별도 축으로 관리한다.

API
- Endpoint: GET /vendors
- Endpoint: GET /vendors/{id}
- Endpoint: POST /vendors

Authorization Scope
- vendor.read
- vendor.write

향후 Endpoint별 권한을 모델링할 경우 Endpoint가 필요한 Scope를 참조하는 방식으로 확장할 수 있다.

따라서 API Endpoint를 Scope라고 부르는 기존 혼용을 제거한다.

## 10. Frontend 변경

API 등록/수정 화면에서 다음 용어를 변경한다.

- Scope -> Endpoint
- Scope Name -> Endpoint Name
- Scopes -> Endpoints

등록 화면은 다음 구조를 사용한다.

Public Base: /capi/v1/vendors

Endpoints
- GET / -> upstream
- GET /{id} -> upstream
- POST / -> upstream

Endpoint 중복 검증 UI에서도 Endpoint라는 용어를 사용한다.

## 11. OpenAPI 및 문서

GET /capi/v1/vendors, GET /capi/v1/vendors/{id}, POST /capi/v1/vendors 등은 모두 Endpoint로 표현한다.

OAuth Scope는 별도의 권한 개념으로 문서화한다.

## 12. 테스트

Backend 테스트에서 API Endpoint를 의미하는 기존 Scope 명칭을 Endpoint로 변경한다.

필수 테스트:
- Endpoint 생성/조회/수정/삭제
- Endpoint 중복
- Path parameter canonicalization
- Public Base + Path Pattern 조합
- Endpoint별 Upstream
- APISIX Route 생성 및 reconciliation
- PATCH 시 Endpoint 전체 교체

다음 권한 테스트는 Scope 명칭을 유지한다.
- requested scope
- granted scope
- PAT scopes
- OAuth scope authorization
- scope mismatch

## 13. 검색 및 치환 주의사항

프로젝트 전체에서 다음 키워드를 검색한다.

- api_scope
- scope_id
- scope_name
- scopeName
- scopes
- _scope_row_dicts
- _assert_valid_scopes

검색 결과를 Endpoint 의미와 OAuth/PAT Scope 의미로 분류한 후 변경한다.

다음은 변경하지 않는다.

- requested_scopes
- granted_scopes
- pat.scopes
- OAuth scope
- authorization scope

특히 scopes는 문맥을 확인한 후 변경한다.

## 14. 주요 변경 파일

- services/portal-backend/routers/catalog.py
- services/portal-backend/apisix_client.py
- infra/postgres/init/01_ddl.sql
- Frontend API 등록/수정 화면
- 관련 테스트

프로젝트 전체 검색으로 추가 참조를 확인한다.

## 15. 배포 순서

1. DB migration
2. Backend Endpoint 모델 전환
3. APISIX Client 전환
4. Frontend payload/UI 전환
5. 회귀 테스트
6. 기존 API Endpoint 의미의 Scope 명칭 제거

운영 중에는 DB migration과 애플리케이션 배포 사이의 호환성을 확인한다.

## 16. 완료 기준

- [ ] cdp.api_scope -> cdp.api_endpoint
- [ ] scope_id -> endpoint_id
- [ ] scope_name -> endpoint_name
- [ ] API 등록 Request scopes -> endpoints
- [ ] API 조회 Response scopes -> endpoints
- [ ] Backend 내부 변수/함수 Endpoint 용어 통일
- [ ] APISIX Client Endpoint 기반 명칭 통일
- [ ] 기존 Route ID 규칙 유지
- [ ] Endpoint canonicalization 및 중복 방지 유지
- [ ] Frontend Endpoint 용어 적용
- [ ] OAuth/PAT Scope 명칭과 의미 유지
- [ ] 전체 회귀 테스트 통과
- [ ] API Endpoint 의미의 불필요한 Scope 잔존 없음

## 17. 최종 원칙

API Portal의 핵심 모델을 다음과 같이 정의한다.

API Catalog
  -> Public Base
  -> Endpoints
       -> Method
       -> Path Pattern
       -> Upstream
       -> APISIX Route

권한은 별도의 축으로 관리한다.

Endpoint
  -> Authorization
       -> OAuth/PAT Scope

최종적으로 실제 호출 단위는 Endpoint, 접근 권한의 범위는 Scope로 명확히 분리한다.