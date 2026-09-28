# 시민개발자 API 포털 보안 보완점 및 조치계획

- 문서 ID: SA-SEC-CDP-004
- 대상 시스템: tuchy81/api-portal
- 대상 브랜치: main
- 작성 기준일: 2026-09-28
- 문서 상태: POC 보안 보완 계획
- 검토 기준 커밋: 424632f69e1da6714319d9cfa57516dfc7c3242f
- 상위 문서:
  - 01_시민개발자_API포털_아키텍처정의서.md
  - 02_시민개발자_API포털_구현상세스펙.md
  - apisix_API_Endpoint_최종개발스펙.md
  - docs/security_headers.md

---

## 1. 목적 및 전제

본 문서는 현재 구현된 시민개발자 API 포털 POC의 코드, 아키텍처 정의서, 구현 상세 스펙을 기준으로 식별한 보안 보완점과 단계별 조치계획을 정의한다.

현재는 POC 단계이므로 운영 수준의 모든 보안요건을 즉시 완성하는 것이 목적은 아니다. 대신 다음 원칙으로 관리한다.

1. POC 단계에서도 실제 악용 가능성이 높은 구조적 취약점은 우선 제거한다.
2. 개발 편의 설정은 DEV 전용으로 명시하고 운영 배포를 차단한다.
3. Pilot/상용화 전에는 인증·인가·Secret·Network Boundary·Gateway 관리면을 운영 수준으로 강화한다.
4. 이미 구현된 PAT, Scope, Quota, Audit, Header Sanitization은 유지하고 보안 회귀 테스트를 확대한다.

---

## 2. 종합 판단

현재 POC는 보안을 고려한 기반이 상당 부분 구현되어 있다.

- PAT secret 원문 미저장, SHA-256/HMAC 기반 검증
- HMAC constant-time comparison
- Redis cache miss 시 DB fallback 및 negative cache
- X-Citizen-*, X-User-Sub, X-Forwarded-User 등의 spoofable header 제거
- Endpoint 단위 Scope 검사
- Quota atomicity 테스트
- TXS/내부 API 무인증 접근 차단 테스트
- API Owner의 자기 API 수정 권한 검증

다만 다음 3개는 상용화 전에 반드시 보완해야 할 핵심 구조적 이슈다.

| ID | 항목 | 우선순위 | 핵심 이유 |
|---|---|---:|---|
| SEC-01 | Endpoint upstreamUrl 기반 SSRF / 임의 Proxy | P0 | API Owner가 Gateway의 네트워크 권한을 이용해 임의 대상 접근을 유도할 수 있음 |
| SEC-02 | Token Exchange Service 신뢰경계 | P0 | internal authentication이 탈취되면 임의 userSub/scopes 기반 JWT 발급 위험 |
| SEC-03 | Application 승인 BOLA/IDOR | P0 | api-owner 역할만 가진 사용자가 다른 API Owner의 신청을 승인/반려할 수 있음 |

---

# 3. 보안 보완점 상세

## SEC-01. Endpoint upstreamUrl 기반 SSRF / 임의 Proxy

### 현황

API 등록 시 Endpoint별 upstreamUrl을 입력받고, Portal Backend가 APISIX Route의 upstream node로 직접 반영한다.

관련 구현:
- services/portal-backend/routers/catalog.py
- services/portal-backend/apisix_client.py

현재 URL 검증은 절대 URL 형식, fragment, path parameter 정합성 중심이며, 최종적으로 parsed.netloc이 APISIX upstream node에 들어간다.

구조:

~~~text
API Owner
   │ upstreamUrl
   ▼
Portal Backend
   │ APISIX Admin API
   ▼
APISIX
   │
   └──▶ upstreamUrl
~~~

### 위험

API Owner가 Gateway에서 접근 가능한 임의 목적지를 지정하면 다음 대상에 접근할 가능성이 있다.

- 내부 업무 시스템
- Redis / PostgreSQL
- Keycloak
- APISIX Admin / etcd
- Kubernetes/Docker 내부 서비스
- Cloud Metadata endpoint
- 내부 관리용 HTTP API

OWASP API Security Top 10의 API7:2023 Server-Side Request Forgery와 직접 연관되는 영역이다.

### POC 최소 조치

1. upstreamUrl Allowlist 적용
2. http/https 이외 scheme 거부
3. loopback / link-local / RFC1918 / metadata / localhost / 관리용 port 차단
4. DNS resolve 후 실제 IP도 정책으로 검증
5. redirect를 따를 경우 최종 destination도 재검증
6. SSRF 회귀 테스트 추가

### Pilot/상용화 권장 구조

사용자가 URL을 직접 입력하지 않도록 변경한다.

~~~text
API Endpoint
  └─ upstreamServiceId = MDM-VENDOR-SVC

Service Registry
  ├─ MDM-VENDOR-SVC → 내부 Gateway/Service
  ├─ ERP-VENDOR-SVC → 내부 Gateway/Service
  └─ ...
~~~

즉 API Owner가 지정하는 것은 URL이 아니라 등록된 Upstream Service ID이며 실제 접속 주소는 플랫폼 관리 영역에서 결정한다.

### 완료 기준

- [ ] 운영용 API 등록에서 arbitrary upstreamUrl 제거
- [ ] 허용된 Service ID/Host만 등록 가능
- [ ] private/link-local/metadata/admin network 접근 차단 테스트 통과
- [ ] DNS rebinding 및 redirect 우회 테스트 통과
- [ ] Gateway outbound network deny-by-default

---

## SEC-02. Token Exchange Service 신뢰경계 강화

### 현황

services/token-exchange-service/caller_auth.py는 X-Internal-Key와 IP allowlist를 사용한다.

Token Exchange 요청에는 다음 값이 전달된다.

~~~json
{
  "tokenId": "...",
  "userSub": "...",
  "scopes": ["..."]
}
~~~

현재 TXS는 caller 인증 후 이 값을 token exchange로 전달한다.

~~~text
PAT 검증
   ↓
APISIX
   ↓
shared internal key
   ↓
TXS
   ↓
userSub / scopes
   ↓
Keycloak impersonation
~~~

### 위험

internal authentication이 우회되거나 탈취되면 TXS가 임의 사용자 JWT를 발급하는 impersonation service가 될 수 있다.

특히 다음 값에 대한 의미 검증을 TXS 자체에서도 수행해야 한다.

- tokenId
- userSub
- scopes
- PAT status
- PAT expiry

### POC 최소 조치

TXS가 다음을 재검증한다.

1. tokenId가 실제 ACTIVE PAT인지 확인
2. PAT user_sub와 요청 userSub가 일치하는지 확인
3. requested scopes가 PAT granted scopes의 subset인지 확인
4. PAT expiry/status 재검증
5. 현재 Endpoint policy와 requested scope가 일치하는지 확인
6. 실패 시 token exchange 수행 금지

### 상용화 권장 구조

Shared Secret 단일 통제보다 다음과 같은 workload/service identity를 사용한다.

- mTLS
- Kubernetes ServiceAccount/Workload Identity
- 내부 signed assertion
- service-to-service OAuth2 client credentials

권장 흐름:

~~~text
APISIX
  │ service identity + signed assertion
  ▼
TXS
  ├─ assertion signature 검증
  ├─ PAT subject/scope/status/expiry 재검증
  ├─ audience 검증
  └─ replay 방지
       ▼
    Keycloak
~~~

### 완료 기준

- [ ] tokenId ↔ userSub 일치성 검증
- [ ] requested scopes가 허용범위를 초과하지 않음을 검증
- [ ] caller authentication을 service identity 기반으로 강화
- [ ] replay 방지 또는 짧은 수명의 signed assertion 적용
- [ ] 발급 JWT의 sub/aud/scope/azp/exp/jti 자동 검증

---

## SEC-03. Application 승인 API의 BOLA

### 현황

services/portal-backend/routers/applications.py의 PATCH /applications/{app_id}는 호출자가 api-owner 또는 platform-admin인지 확인한다.

하지만 api-owner의 경우 해당 신청 건의 API 소유자인지 다시 확인하지 않는다.

반면 목록 조회에서는 API Owner별 owner_sub 조건을 적용하고 있다.

### 공격 경로

~~~text
API Owner A
  │
  └─ PATCH /applications/{B의 applicationId}
           │
           └─ APPROVE / REJECT
~~~

OWASP API1:2023 Broken Object Level Authorization에 해당하는 전형적인 객체 소유권 검증 누락 유형이다.

### 조치

승인/반려 권한을 다음으로 제한한다.

~~~text
platform-admin
    OR
application.api.owner_sub == current_user.sub
~~~

가능하면 DB 조회 단계에서 ownership 조건을 함께 적용한다.

### 완료 기준

- [ ] API Owner A가 B의 신청을 승인할 수 없음
- [ ] API Owner A가 B의 신청을 반려할 수 없음
- [ ] Platform Admin은 전체 승인/반려 가능
- [ ] cross-owner authorization 테스트 추가

---

## SEC-04. BFF 설계와 Frontend localStorage 인증 불일치

### 현황

아키텍처 문서는 브라우저에 세션 쿠키만 유지하는 BFF 구조를 정의하지만 현재 frontend/portal-web/src/api/axios.ts는 localStorage에서 cdp_auth_token을 읽어 Authorization header에 넣는다.

~~~text
localStorage.cdp_auth_token
        ↓
Authorization: Bearer <token>
~~~

OWASP Session Management Cheat Sheet는 인증 토큰/JWT/refresh token 등을 localStorage/sessionStorage에 저장하지 말고 HttpOnly/Secure/SameSite Cookie 또는 BFF 패턴을 사용하도록 권고한다.

### POC 판단

로컬 개발 편의를 위한 mock authentication이면 POC에서는 허용할 수 있다.

단 운영 배포에서는:

- BFF session 사용
- Browser는 HttpOnly + Secure + SameSite cookie만 보유
- state-changing request에 CSRF 방어
- localStorage token interceptor 제거

### 완료 기준

- [ ] Production build에 localStorage token 없음
- [ ] HttpOnly + Secure + SameSite cookie 적용
- [ ] CSRF 방어 적용
- [ ] Browser JavaScript가 access token을 직접 읽을 수 없는 구조

---

## SEC-05. Repository 내 기본 Secret 및 개발 Credential

### 확인 대상

- PostgreSQL password
- Redis password
- Keycloak admin credential
- Keycloak client secret
- APISIX Admin API key
- Portal SERVER_KEY
- INTERNAL_API_KEY

주요 파일:
- docker-compose.yml
- services/portal-backend/config.py
- services/token-exchange-service/config.py
- services/apisix-gateway/conf/config.yaml
- infra/keycloak/hd-realm.json

### POC 판단

개발환경 전용 Credential이면 POC 자체에서는 허용할 수 있다. 단 운영에 동일 값을 재사용하면 안 된다.

### 조치

1. 환경변수/Secret Manager/Kubernetes Secret으로 이동
2. production secret의 Git 저장 금지
3. CI secret scanning 적용
4. POC secret을 운영에서 재사용하지 않도록 교체
5. Keycloak service account secret 별도 보호
6. APISIX Admin key는 관리망에서만 사용

### 완료 기준

- [ ] production secret이 Git에 없음
- [ ] secret scanning CI 통과
- [ ] Secret rotation 절차 정의
- [ ] Dev/Test/Prod Secret 분리

---

## SEC-06. Management Plane 및 개발용 Network Exposure

현재 docker-compose.yml은 PostgreSQL, Redis, Keycloak, TXS, Portal Backend, APISIX, APISIX Admin, Dashboard, Monitoring 등을 host port에 노출한다.

APISIX는 현재 Admin API를 0.0.0.0:9180으로 listen하고 allow_admin도 0.0.0.0/0 개발 설정이다. etcd 역시 개발 설정에서 인증 없이 사용한다.

### 조치

POC:
- localhost 바인딩 우선
- 외부 접근이 필요 없는 서비스는 host port publish 제거
- APISIX Admin/etcd/Redis/Postgres는 외부 접근 차단
- Dashboard는 관리자 전용망/VPN에서만 접근

운영:
- Data Plane과 Management Plane 분리
- Admin API/etcd는 별도 관리망
- 방화벽/NetworkPolicy로 deny-by-default

---

## SEC-07. X-Forwarded-For 신뢰 경계

PAT allowed_cidr와 TXS caller IP 검증이 X-Forwarded-For에 의존한다.

관련 구현:
- services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua
- services/token-exchange-service/caller_auth.py

### 위험

신뢰되지 않은 proxy chain에서 client가 X-Forwarded-For를 직접 전달할 수 있으면 IP 제한을 우회할 수 있다.

### 조치

- 외곽 LB가 기존 XFF를 제거하고 새 값 설정
- APISIX/TXS는 trusted proxy에서 전달된 XFF만 신뢰
- 직접 연결은 source IP 기반 처리
- 필요 시 PROXY protocol 사용

### 완료 기준

- [ ] 임의 XFF로 allowed_cidr 우회 불가
- [ ] trusted proxy 목록 명시
- [ ] XFF spoofing 회귀 테스트 추가

---

## SEC-08. Keycloak Token Exchange Scope/Audience 검증

현재 realm 설정에는 fullScopeAllowed, service account impersonation, token exchange 권한 등이 포함되어 있다.

설정만으로 실제 취약점이 확정되는 것은 아니며, **실제로 발급되는 JWT를 기준으로 검증해야 한다.**

필수 시나리오:

| 시나리오 | 기대 결과 |
|---|---|
| PAT scope=read, request=read | 허용 |
| PAT scope=read, request=write | 거부 |
| PAT에 없는 scope request | 거부 |
| 다른 userSub 지정 | 거부 |
| 다른 audience 지정 | 거부 |
| 만료 PAT | 거부 |
| 폐기 PAT의 cached JWT | 정의된 TTL 정책 내 일관되게 통제 |

검증 claim:

~~~text
sub
aud
azp
scope
realm_access.roles
exp
iat
jti
~~~

---

## SEC-09. CORS와 CSRF 정책

CORS는 인증/인가의 대체 수단이 아니다.

조치:
- Portal UI origin 명시 allowlist
- Access-Control-Allow-Origin: * 금지
- credential 요청은 명시된 trusted origin만 허용
- state-changing BFF 요청 CSRF 방어
- Swagger/API 문서용 CORS 정책 분리

---

## SEC-10. API Catalog/OpenAPI 정보 노출

GET /catalog/apis/{api_id}는 API 상세정보와 Endpoint 정보를 반환한다.

검토 대상:
- upstream URL
- 내부 host/service name
- OpenAPI schema
- example request/response
- 내부 시스템명
- 민감 필드 정의

### 조치

사용자용 DTO와 관리자용 DTO를 분리한다.

~~~text
Citizen Catalog DTO
  ├─ publicPath
  ├─ endpoint
  ├─ requiredScope
  └─ description

Admin DTO
  ├─ upstreamUrl
  ├─ internal service id
  ├─ deployment metadata
  └─ operational information
~~~

OpenAPI 예제에는 실제 Credential과 개인정보가 포함되지 않도록 등록 시 검증 또는 masking 정책을 적용한다.

---

## SEC-11. Error/Log 정보 최소화

운영에서는 다음을 외부 오류 응답에 노출하지 않는다.

- DB error detail
- internal hostname
- Redis 상태
- Keycloak URL
- APISIX Admin endpoint
- upstream URL 전체
- 내부 authentication 처리 과정

외부에는 표준 problem+json 오류코드만 제공하고 상세 원인은 log/trace로 관리한다.

---

## SEC-12. 이미지/의존성 공급망 보안

현재 Docker Compose에는 latest tag 기반 이미지가 존재한다.

예:
- bitnamilegacy/etcd:latest
- prom/prometheus:latest

상용화 전 다음을 적용한다.

- 이미지 버전 고정
- 가능하면 digest pinning
- Container CVE scanning
- Python/Node dependency scanning
- lock file 검증
- non-root container 검토
- SBOM 생성

---

# 4. POC 단계 최소 조치

POC에서 운영 보안을 모두 완성할 필요는 없지만 다음은 즉시 반영을 권장한다.

| 우선 | 조치 | 목적 |
|---:|---|---|
| 1 | Upstream URL Allowlist | SSRF 위험 축소 |
| 2 | Application ownership check | BOLA 제거 |
| 3 | TXS tokenId/userSub/scope 재검증 | impersonation 경계 강화 |
| 4 | INTERNAL_API_KEY 미설정 시 TXS 기동 실패 | 실수로 무인증 TXS 노출 방지 |
| 5 | APISIX Admin/Redis/Postgres/Keycloak host port 최소화 | 관리면 노출 축소 |
| 6 | Dev Secret과 운영 Secret 분리 | 재사용 방지 |
| 7 | XFF trusted proxy 정책 | CIDR 우회 방지 |
| 8 | SSRF/BOLA/TXS abuse 테스트 추가 | 회귀 방지 |

---

# 5. Pilot 전 필수 보완

### 인증/인가

- [ ] BFF HttpOnly Cookie
- [ ] CSRF 방어
- [ ] PAT Scope 서버 검증
- [ ] API Owner object-level authorization
- [ ] TXS service identity
- [ ] Keycloak token exchange fine-grained policy 검증
- [ ] JWT iss/aud/sub/exp 검증

### Gateway

- [ ] Upstream Service Registry
- [ ] outbound network allowlist
- [ ] Admin API private network
- [ ] XFF trusted proxy
- [ ] Route 변경 감사

### 데이터

- [ ] DB/Redis Secret 분리
- [ ] PAT 평문 미저장 확인
- [ ] Audit log 접근권한 최소화
- [ ] OpenAPI 민감정보 검사

### 운영

- [ ] Secret rotation
- [ ] CI security scanning
- [ ] Container image scanning
- [ ] SBOM
- [ ] 보안 회귀 테스트
- [ ] 장애 시 Fail-Closed 검증

---

# 6. 상용화 목표 아키텍처

~~~mermaid
flowchart LR
    B["Browser / Script / RPA"]
    BFF["Portal BFF / HttpOnly Session"]
    PORTAL["Portal Backend / Control Plane"]
    REG["Upstream Service Registry"]
    CGW["Citizen API Gateway / APISIX"]
    TXS["Token Exchange Service / Service Identity"]
    KC["Keycloak / OIDC / Token Exchange"]
    IGW["Internal API Gateway"]
    PDP["Authorization Service"]
    API["Domain APIs"]

    B --> BFF
    BFF --> PORTAL
    PORTAL --> REG
    PORTAL --> CGW
    B -->|"PAT"| CGW
    CGW -->|"Signed Assertion"| TXS
    TXS --> KC
    TXS --> CGW
    CGW -->|"JWT"| IGW
    IGW --> PDP
    IGW --> API
    REG --> CGW
~~~

핵심 변경:

현재:
~~~text
API Owner → arbitrary upstreamUrl → APISIX
~~~

목표:
~~~text
API Owner → upstreamServiceId
                     ↓
              Service Registry
                     ↓
                  APISIX
~~~

현재:
~~~text
APISIX → shared key → TXS → requested userSub/scopes
~~~

목표:
~~~text
APISIX → service identity + signed assertion
             ↓
            TXS
             ↓
   PAT / subject / scope 재검증
             ↓
          Keycloak
~~~

---

# 7. 보안 테스트 계획

현재 tests/security/test_security.py에는 다음 테스트가 이미 존재한다.

- SEC-01 PAT plaintext log leakage
- SEC-02 Header spoofing
- SEC-03 Quota atomicity
- SEC-04 Negative cache
- SEC-05 TXS unauthenticated caller rejection
- SEC-06 Internal endpoint unauthenticated rejection

추가 테스트:

## 7.1 Authorization

~~~text
SEC-07 API Owner cross-owner application approval
SEC-08 API Owner cross-owner application rejection
SEC-09 Cross-owner API modification
SEC-10 Platform-admin override
~~~

## 7.2 SSRF

~~~text
SEC-11 private IP upstream registration rejection
SEC-12 loopback upstream rejection
SEC-13 link-local / metadata rejection
SEC-14 disallowed port rejection
SEC-15 DNS resolving to private IP rejection
SEC-16 redirect-to-private-target rejection
~~~

## 7.3 TXS

~~~text
SEC-17 tokenId/userSub mismatch rejection
SEC-18 PAT scope escalation rejection
SEC-19 inactive PAT rejection
SEC-20 expired PAT rejection
SEC-21 arbitrary user impersonation rejection
SEC-22 audience tampering rejection
~~~

## 7.4 Proxy/Network

~~~text
SEC-23 spoofed X-Forwarded-For rejection
SEC-24 direct TXS access rejection
SEC-25 direct APISIX Admin access rejection
SEC-26 direct Portal internal endpoint access rejection
~~~

---

# 8. 조치계획

| 단계 | 주요 조치 | 완료 판단 |
|---|---|---|
| Phase 0 / 현재 POC | P0 3건 수정, SSRF/BOLA/TXS 보안 테스트, Dev Secret 분리, 관리면 축소 | 핵심 구조적 취약점 제거 |
| Phase 1 / Pilot 준비 | Upstream Registry, service identity, BFF cookie, network segmentation, Secret Manager | 실제 내부 API 연결 가능 |
| Phase 2 / 운영 전 | Keycloak exchange 정책 확정, mTLS 또는 Workload Identity, CI Security Gate, image/SBOM, 관제 | 운영 보안 기준 충족 |
| Phase 3 / 운영 | 정기 취약점 진단, Secret rotation, 권한 review, API inventory review, incident response | 지속적 보안관리 |

---

# 9. Security Gate

## Gate A — POC 종료

- [ ] SEC-01 SSRF 통제
- [ ] SEC-02 TXS impersonation 방어
- [ ] SEC-03 BOLA 방어
- [ ] SEC-07 XFF trust boundary
- [ ] 보안 회귀 테스트 자동화
- [ ] Production과 Dev Credential 분리

## Gate B — Pilot 시작

- [ ] Upstream Service Registry
- [ ] 관리면 네트워크 차단
- [ ] BFF/HttpOnly Cookie
- [ ] Service Identity
- [ ] Secret Manager
- [ ] Keycloak token exchange 정책 검증
- [ ] 보안 모니터링/감사 연계

## Gate C — Production 전환

- [ ] 침투테스트 또는 보안진단
- [ ] CI Security Gate
- [ ] Container/Dependency CVE 기준
- [ ] 운영 Secret rotation 정책
- [ ] 관리자 권한 review
- [ ] API Inventory / retired API 관리
- [ ] Incident response 및 audit retention 정책

---

# 10. 우선순위 요약

| ID | 위험 | 현재 판단 | 조치 시점 |
|---|---|---|---|
| SEC-01 | 임의 Upstream / SSRF | Critical | POC 즉시 |
| SEC-02 | TXS impersonation trust boundary | Critical | POC 즉시 |
| SEC-03 | Application approval BOLA | Critical | POC 즉시 |
| SEC-04 | localStorage JWT | High | Pilot 전 |
| SEC-05 | hardcoded/default secrets | High | Pilot 전 |
| SEC-06 | Management plane exposure | High | POC 네트워크부터 완화 |
| SEC-07 | XFF spoofing | High | POC |
| SEC-08 | Keycloak scope/audience 검증 | High | Pilot 전 |
| SEC-09 | CORS/CSRF | Medium | Pilot 전 |
| SEC-10 | Catalog/OpenAPI 정보 노출 | Medium | Pilot 전 |
| SEC-11 | Error/log leakage | Medium | Pilot 전 |
| SEC-12 | Dependency/image pinning | Medium | Production 전 |

---

# 11. 결론

현재 구현은 POC로서는 PAT 인증, Scope, Quota, Audit, Header Sanitization 등 보안 기반이 상당 부분 들어가 있는 상태다.

다만 핵심은 단순히 인증 기능이 존재하는지가 아니라 **각 컴포넌트가 어떤 입력을 신뢰하는지와 Gateway가 어떤 네트워크 권한을 가지는지**이다.

따라서 우선순위는 다음과 같다.

1. Gateway가 사용자가 입력한 Upstream URL을 실행하지 않도록 할 것
2. Token Exchange가 caller 전달값을 그대로 신뢰하지 않도록 할 것
3. 관리 기능에서 역할 보유와 객체 소유권을 분리해 검증할 것
4. 그 다음 BFF Session, Secret Management, Network Isolation, Keycloak Exchange Policy를 운영 수준으로 강화할 것

---

# 12. 참고 기준

- OWASP API Security Top 10 2023
  - API1:2023 Broken Object Level Authorization
  - API2:2023 Broken Authentication
  - API7:2023 Server Side Request Forgery
  - API8:2023 Security Misconfiguration
  - API9:2023 Improper Inventory Management
- OWASP Server-Side Request Forgery Prevention Cheat Sheet
- OWASP Session Management Cheat Sheet
- OWASP HTML5 Security Cheat Sheet

참고 링크:

- https://owasp.org/projects/api-security-project
- https://api-security.owasp.org/editions/2023/en/0x11-t10/
- https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html
