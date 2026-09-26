# APISIX 브랜치 종합 분석 보고서

- Repository: tuchy81/api-portal
- 분석 브랜치: apisix
- 분석 대상: 시민개발자 API 포털 / APISIX Gateway / Token Exchange Service
- 분석 관점: 아키텍처 적합성, 보안, 인증·인가, 장애 대응, 운영성, 코드 구현 정합성
- 분석 기준: apisix 브랜치의 설계 문서와 실제 구현 코드

## 1. 종합의견

이번 apisix 브랜치는 단순한 API Gateway PoC를 넘어 시민개발자용 API 접근 경로를 기존 엔터프라이즈 인증·인가 체계와 연결하기 위한 보안·운영 아키텍처를 실제 코드 수준까지 구체화하고 있습니다.

핵심 구조는 다음과 같습니다.

시민개발자 → PAT → APISIX Citizen API Gateway → Token Exchange Service → Keycloak → Internal JWT → 기존 Internal API Gateway → PDP → Domain API

가장 중요한 장점은 PAT를 내부 시스템까지 전달하지 않고 APISIX에서 종료시키며, 내부 영역에서는 기존 JWT + Internal Gateway + PDP 모델을 그대로 재사용한다는 점입니다.

즉, 시민개발자라는 새로운 호출 주체를 추가하면서도 기존 내부 API의 인증·인가 모델을 크게 변경하지 않는 구조입니다. 차세대 시스템의 표준화 관점에서도 재사용성과 확장성이 높은 방향입니다.

## 2. 칭찬할 부분

### 2.1 기존 보안 체계를 최대한 재사용

Keycloak, Internal Gateway, PDP를 새 플랫폼에서 다시 구현하지 않고 연결 계층으로 활용한 점이 좋습니다. 새로운 인증·인가 섬을 만들지 않고 기존 엔터프라이즈 보안 경계에 시민개발자 경로를 접목했습니다.

### 2.2 PAT와 내부 JWT의 Trust Boundary가 명확

PAT는 시민개발자 영역에서만 이해하고 내부 Zone으로 넘어갈 때 JWT로 변환합니다. 따라서 Domain API가 PAT라는 새로운 credential을 알 필요가 없습니다.

### 2.3 이중 인가 구조

APISIX에서 Coarse-grained Scope를 검사하고 Internal Gateway와 PDP에서 Fine-grained Authorization을 수행하는 구조가 명확합니다. 1차 Scope 통과가 2차 업무 권한 검사를 우회하지 않는 구조입니다.

### 2.4 Token Exchange Service 분리

APISIX와 Keycloak 사이의 RFC 8693 Token Exchange를 별도 서비스로 분리한 것은 책임 분리 측면에서 좋습니다. Keycloak 연계, 캐시, Single-flight, Circuit Breaker를 독립적으로 운영할 수 있습니다.

### 2.5 Redis 기반 Rate Limit / Quota의 원자적 처리

pat-quota.lua에서 Redis Lua/EVALSHA를 이용하여 TPS와 일·월 Quota를 원자적으로 처리하는 방식은 동시 요청 환경에 적합합니다.

### 2.6 Token Exchange Cache의 운영 고려

cache.py에는 JWT Cache, Distributed Single-flight Lock, Probabilistic Early Refresh, Circuit Breaker와 같은 운영 요소가 들어가 있습니다. 특히 Token Exchange Storm을 줄이기 위한 Single-flight 구현은 실제 운영 상황을 고려한 좋은 부분입니다.

### 2.7 Header Trust Boundary를 의식한 구현

클라이언트가 임의로 넣은 X-Citizen- 계열 Header와 사용자 식별 Header를 제거하고 Gateway가 신뢰 가능한 값을 재주입하는 방식은 보안 경계를 코드로 표현했다는 점에서 좋습니다.

### 2.8 Audit Correlation

PAT ID, User Subject, JWT JTI, Trace ID, Client IP, HTTP Method, Request Path, Status, Latency, Error Code를 연결하려는 방향은 장애 분석과 보안 감사에 유용합니다.

## 3. 보완할 부분

### 3.1 P0 — PAT Cache Miss 경로의 인증 우회 가능성

가장 중요한 보완사항입니다.

현재 pat-auth.lua의 Redis Cache Hit 경로에서는 저장된 HMAC/hash를 이용해 secret을 검증하지만, Redis Cache Miss 후 Portal Backend fallback에서 hash가 빈 값으로 반환되는 경우 HMAC 검증 자체가 수행되지 않을 가능성이 있습니다.

개념적으로 다음과 같은 차이가 생깁니다.

Redis HIT → hash 존재 → HMAC 검증
Redis MISS → Backend fallback → hash 빈 값 → HMAC 검증 조건 미진입 가능

이는 인증 로직의 Fail-Open 문제로 볼 수 있으며 최우선 수정이 필요합니다.

권고사항:

1. Portal Backend fallback에서도 평문 secret이 아니라 검증용 HMAC/hash를 반드시 반환합니다.
2. Cache Hit와 Cache Miss에서 동일한 HMAC 검증을 수행합니다.
3. hash가 없으면 인증 실패로 처리합니다.
4. 빈 hash를 의미 있는 검증 생략 신호로 사용하지 않습니다.

### 3.2 P1 — X-Forwarded-For Trust Boundary

pat-auth.lua와 TXS caller_auth.py 모두 X-Forwarded-For를 Client IP 판단에 사용합니다. 외부 클라이언트가 XFF를 직접 주입할 수 있는 환경이라면 CIDR 제한을 우회할 수 있습니다.

권고사항:

Trusted LB 또는 Reverse Proxy가 기존 XFF를 제거하고 실제 Client IP를 기록하도록 합니다. APISIX와 TXS는 신뢰된 Proxy에서 전달된 XFF만 사용하도록 정책을 통일합니다.

### 3.3 P1 — Scope 정책은 Fail-Closed

Scope mapping 또는 required_scope_map이 없는 경우 허용되는 형태의 로직은 신규 Route 설정 누락 시 보안상 위험할 수 있습니다.

정책이 없으면 허용하는 대신 다음과 같이 처리하는 것이 바람직합니다.

정책 있음 → Scope 평가
정책 없음 → 설정 오류 또는 403

신규 API Route가 계속 추가되는 운영 환경에서는 특히 중요합니다.

### 3.4 P1 — Keycloak Token Exchange Down-scoping 실효성 검증

keycloak_client.py는 requested_subject, audience, scope를 사용하여 RFC 8693 Token Exchange를 수행합니다. 구조는 적절하지만 실제 Keycloak 설정에서 의도한 사용자 대리발급과 Scope 축소가 보장되는지는 코드만으로 판단할 수 없습니다.

다음 PoC를 필수 검증 항목으로 두는 것을 권고합니다.

1. 권한이 많은 사용자에게 일부 Scope만 가진 PAT를 발급
2. Token Exchange 후 JWT의 scope와 role 확인
3. PAT에 없는 Scope 요청
4. 다른 사용자 subject 요청
5. 허용되지 않은 audience 요청

### 3.5 P1 — Token Exchange Service 보호 강화

TXS는 사용자 impersonation JWT를 발급할 수 있는 고위험 보안 컴포넌트입니다. 현재 X-Internal-Key와 CIDR Allowlist를 사용하는 방향은 좋지만 운영 환경에서는 NetworkPolicy와 mTLS 또는 강한 Service Identity까지 고려할 가치가 있습니다.

권고 모델:

APISIX → NetworkPolicy → mTLS/Service Identity → TXS

### 3.6 P2 — JWT Claim Header의 신뢰 경계

pat-token-exchange.lua에서는 JWT payload를 decode하여 사용자 관련 Header를 생성합니다. 최종 JWT 서명 검증은 Internal Gateway에서 수행하므로 구조 자체는 이해할 수 있습니다.

다만 APISIX가 생성한 X-User-* 계열 Header는 Security Authority가 아니라 Context 정보로 정의해야 합니다. 실제 권한 판단은 Internal Gateway에서 JWT 서명, issuer, audience, expiry를 검증한 이후의 Trusted Claim과 PDP 정책을 기준으로 수행해야 합니다.

가능하면 Internal Gateway가 검증된 JWT를 기반으로 필요한 사용자 Context Header를 다시 생성하는 것도 고려할 수 있습니다.

### 3.7 P2 — Scope Hash를 8자리로 축약

현재 JWT Cache Key의 Scope Hash는 SHA-256 결과를 8자리 hexadecimal로 축약하여 32bit 식별자로 사용합니다.

권한 범위를 나타내는 Cache Key이므로 불필요한 충돌 가능성을 제거하기 위해 전체 SHA-256을 사용하는 것을 권고합니다.

### 3.8 P2 — JWT Cache 권한 변경 반영 정책

JWT Cache TTL이 약 240초 수준인 것은 운영상 합리적입니다. 다만 Keycloak Role 변경, PAT Scope 변경, API Scope 변경, 사용자 비활성화, PDP 정책 변경을 어느 시점에 반영할지 명확한 정책이 필요합니다.

즉시 반영이 필요한 정책은 명시적 Cache Invalidation을 사용하고, 그렇지 않은 정책은 최대 TTL 이내 반영으로 정의하는 것이 좋습니다.

### 3.9 P2 — 보안 Audit의 Durable 처리

pat-audit.lua는 worker-local batch 기반이며 전송 실패 시 이벤트가 유실될 수 있습니다. 일반 운영 로그에는 허용될 수 있지만 PAT 인증 실패, Scope Denied, Token Exchange, Authorization Denied와 같은 보안 감사 이벤트는 별도 기준이 필요합니다.

권고 모델:

APISIX → Redis Stream 또는 Kafka → Audit Consumer → PostgreSQL/SIEM

Phase 1에서는 이미 사용 중인 Redis를 활용하여 Redis Stream으로 시작하는 것도 현실적입니다.

### 3.10 P3 — Quota 계수 기준 명문화

현재 인증과 Scope 검사를 통과한 후 Quota를 차감하므로 이후 Token Exchange 또는 내부 API가 실패하더라도 호출량이 소진될 수 있습니다.

이것은 반드시 버그는 아니며 정책의 문제입니다. 인증과 Scope 검사를 통과한 API 호출 시도를 1회로 볼 것인지, Domain API의 성공 응답만 사용량으로 볼 것인지 명문화해야 합니다.

## 4. 문서와 코드 정합성

설계 문서에서는 PAT secret을 Argon2id로 해시하여 저장한다고 정의되어 있으나 실제 APISIX 인증 경로는 HMAC-SHA256 기반 검증 구조를 사용합니다.

따라서 현재 상태에서는 설계 문서와 구현의 보안 모델이 일치하지 않습니다.

APISIX 요청 경로에서 매 요청 Argon2id 검증을 수행하는 대신 HMAC 기반 검증을 유지하려는 의도라면, 실제 구현을 기준으로 설계 문서를 수정하고 다음을 명확히 정의하는 것이 좋습니다.

- PostgreSQL 원장에 저장하는 검증 값
- Redis에 캐시하는 검증 값
- HMAC 서버 키의 보관 위치
- Secret 평문 미보관 원칙
- Cache Miss fallback에서도 동일한 검증 수행

## 5. 권고하는 최종 보안 모델

Citizen Developer → PAT → APISIX

APISIX 책임:
- PAT Authentication
- Client IP 정책
- Scope Enforcement
- Rate Limit / Quota
- Header Sanitization
- Audit

APISIX → Token Exchange Service → Keycloak

Token Exchange Service 책임:
- RFC 8693
- 사용자 대리발급 정책
- Scope Down-scoping
- JWT Cache
- Single-flight
- Circuit Breaker

Keycloak → Internal JWT → Internal Gateway

Internal Gateway 책임:
- JWT Signature 검증
- issuer / audience / expiry 검증
- Trusted Claim 생성
- PDP 호출

PDP → Domain API

핵심 원칙은 다음과 같습니다.

Citizen Gateway는 PAT를 이해하는 마지막 지점이고, Internal Gateway는 검증된 JWT를 신뢰하는 첫 번째 지점이어야 합니다.

## 6. 우선순위별 보완 로드맵

| 우선순위 | 항목 | 중요도 | 권고 |
|---|---|---|---|
| P0 | PAT Cache Miss 인증 우회 가능성 | 매우 높음 | 즉시 수정 |
| P1 | X-Forwarded-For Trust Boundary | 높음 | 운영 전 필수 |
| P1 | Scope 정책 Fail-Closed | 높음 | 운영 전 필수 |
| P1 | Keycloak Token Exchange Down-scoping PoC | 높음 | 구축 전 검증 |
| P1 | TXS Network/mTLS 보호 | 높음 | 운영 환경 권고 |
| P2 | JWT Claim Header 신뢰경계 | 높음 | 표준화 필요 |
| P2 | JWT Cache Scope Hash 전체값 사용 | 중간 | 개선 권고 |
| P2 | JWT 권한 변경 Invalidation 정책 | 중간 | 정책 확정 |
| P2 | 보안 Audit Durable Queue | 중간 | 운영 단계 권고 |
| P3 | Quota 계수 기준 | 중간 | 정책 명문화 |
| P3 | 설계문서 Argon2id/HMAC 정합성 | 중간 | 문서 수정 |

## 7. 최종 종합의견

이번 apisix 브랜치는 시민개발자 API 플랫폼의 핵심 구조를 실제 코드 수준까지 구체화했다는 점에서 의미 있는 구현입니다.

특히 Credential → Authentication → Coarse Authorization → Quota → Token Exchange → Enterprise Authentication → Fine-grained Authorization → Audit으로 이어지는 전체 보안 체인을 하나의 흐름으로 설계한 점이 좋습니다.

또한 기존 Keycloak / Internal Gateway / PDP를 재사용하기 때문에 차세대 시스템의 기존 보안 아키텍처와 충돌하지 않고 확장할 수 있습니다.

다만 현재 구조를 바로 운영 표준으로 확정하기보다는 다음 네 가지를 우선 통과시키는 것이 필요합니다.

1. PAT Cache Miss 인증 우회 가능성 제거
2. X-Forwarded-For Trust Boundary 확정
3. Scope 정책 Fail-Closed 적용
4. Keycloak Token Exchange의 실제 Down-scoping 결과 검증

이 네 가지를 해결하면 현재 구조는 시민개발자 API 플랫폼의 표준 아키텍처로 발전시키기에 충분한 기반을 갖추고 있습니다.

## 부록. 주요 검토 파일

- services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua
- services/apisix-gateway/plugins/apisix/plugins/pat-quota.lua
- services/apisix-gateway/plugins/apisix/plugins/pat-token-exchange.lua
- services/apisix-gateway/plugins/apisix/plugins/pat-audit.lua
- services/token-exchange-service/cache.py
- services/token-exchange-service/keycloak_client.py
- services/token-exchange-service/caller_auth.py