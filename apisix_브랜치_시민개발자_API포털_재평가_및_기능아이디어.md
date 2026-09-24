# 시민개발자 API 포털 재평가 및 기능 확장 아이디어

- Repository: tuchy81/api-portal
- Branch: apisix
- 대상: 수정·보완된 시민개발자 API 포털 / APISIX 기반 API Platform
- 기준: 기존 제안 중 **의도적으로 선택하지 않은 항목은 재평가에서 제외**
- 목적: 현재 구현 수준을 다시 평가하고, 보안 이외의 시민개발자 관점 기능을 아이디어화

---

## 1. 재평가 결론

현재 apisix 브랜치는 단순한 API Gateway PoC에서 벗어나 **시민개발자용 Self-Service API Platform**의 형태로 발전하고 있습니다.

현재 흐름은 다음과 같습니다.

```text
API Catalog → API 신청 / Application → 승인 → PAT 발급 → APISIX Gateway → Token Exchange → Enterprise API → Usage / Audit
```

특히 이번 수정에서 의미 있는 부분은 다음 네 가지입니다.

1. **PAT Cache Miss 경로의 인증 검증 보완**
2. **Scope 처리의 Fail-Closed 방향 강화**
3. **Audit Buffer의 유실 방지 개선**
4. **API Catalog → APISIX Route 자동 등록 → Smoke Test → Publish/Rollback 흐름 구현**

이 중 특히 4번은 중요합니다. 기존 API 포털이 단순히 API 정보를 보여주고 신청을 받는 수준이었다면, 현재 구조는 **API의 등록·배포·검증까지 연결하는 Control Plane** 성격을 갖기 시작했습니다.

## 2. 수정된 부분에 대한 재평가

### 2.1 PAT Cache Miss 인증 처리 — 개선 완료

기존에 가장 우려했던 Cache Miss 경로의 검증 생략 문제가 수정되었습니다. 이제 Redis Cache Hit와 Cache Miss 모두 검증용 HMAC을 확보하여 동일한 방식으로 Secret을 검증하는 구조입니다.

```text
Redis HIT  → HMAC 조회 → Secret 검증
Redis MISS → Portal Backend 조회 → HMAC 확보 → Secret 검증
```

**평가: 이전 P0 이슈가 해소된 것으로 판단합니다.**

### 2.2 Scope 처리 — Fail-Closed 방향 개선

Path / Method와 Scope의 관계를 더 명확하게 처리하도록 개선되었습니다. 등록된 Scope와 실제 요청 Path가 일치하지 않을 경우 허용하지 않는 방향으로 동작하는 점이 중요합니다.

**평가: 실제 API 소비 환경을 고려한 방향으로 개선되었습니다.**

### 2.3 Audit Buffer — 운영성을 고려한 개선

Audit 이벤트를 worker timer 기반으로 flush하고 shutdown 시에도 잔여 이벤트를 처리하도록 보완되었습니다. 테스트에서 기존 버퍼의 이벤트 손실 가능성을 확인하고 개선 결과를 검증한 점도 좋습니다.

외부 Durable Queue를 선택하지 않았더라도, 현재 단계에서는 **구현 복잡도를 크게 늘리지 않고 유실 가능성을 줄이는 현실적인 개선**으로 볼 수 있습니다.

**평가: 현재 PoC → Pilot 단계에서는 충분히 합리적인 선택입니다.**

### 2.4 API Catalog와 Gateway의 연결 — 가장 중요한 개선

API 등록 후 다음 흐름이 자동화되어 있습니다.

```text
API 등록 → DRAFT → APISIX Route 생성 → Smoke Test
                         ↓
                  성공 → PUBLISHED
                  실패 → Route Rollback → DRAFT
```

이것은 단순한 API Portal이 아니라 **API Lifecycle Control Plane의 시작**입니다.

특히 API 등록과 Gateway 설정의 이중 관리, 수동 Route 등록, 잘못된 Route 노출, Catalog와 Gateway 상태 불일치 문제를 줄입니다.

**평가: 현재 프로젝트의 가장 중요한 아키텍처적 진전입니다.**

## 3. 현재 플랫폼의 성격

현재는 다음 단계에 가깝습니다.

```text
Discover → Understand → Try → Subscribe → Credential → Call → Monitor → Compose → Lifecycle
```

따라서 앞으로의 핵심은 보안을 계속 추가하는 것보다 **시민개발자가 API를 얼마나 쉽게 발견하고 실제 업무에 사용할 수 있는가**로 이동하는 것이 좋습니다.

## 4. 시민개발자 관점 기능 아이디어

### 4.1 API Playground — 최우선 추천

API 상세 화면에서 바로 API를 실행할 수 있도록 합니다.

```text
거래처 기본정보 조회 API
GET /vendors/{vendorId}

vendorId [ 10001234 ]
Authorization: 내 PAT 사용
[ Try It ]

Response
200 OK
{ ... }
```

현재 OpenAPI/Swagger 구조를 활용한다면 **Swagger UI + 내 PAT로 실행**을 MVP로 만들 수 있습니다.

SAP Business Accelerator Hub도 API를 검색·탐색하고 테스트하는 경험을 핵심 기능으로 제공합니다.

### 4.2 Business-oriented API Search

기술적인 API 이름뿐 아니라 업무 용어로 검색할 수 있게 합니다.

예: ‘거래처’를 검색하면 거래처 기본정보, 담당자, 사업자정보, 변경이력 API 등을 보여줍니다.

핵심은 **API Catalog를 기술 문서 검색기가 아니라 업무 기능 검색기로 만드는 것**입니다.

### 4.3 Business-friendly API Description

Endpoint보다 ‘이 API로 무엇을 할 수 있는가’를 먼저 보여줍니다.

예:

```text
거래처 기본정보 조회

무엇을 할 수 있나요?
→ 거래처의 기본정보와 현재 상태를 조회합니다.

활용 예
• 거래처 조회 화면
• Excel 거래처 목록
• RPA 거래처 검증
• 구매 업무 자동화
• 사내 챗봇
```

### 4.4 Copy & Run

현재 Repository의 curl / Python 예제 구조를 Portal UX로 끌어올리는 기능입니다.

```text
[ Copy cURL ] [ Python ] [ JavaScript ] [ PowerShell ] [ Run in Playground ]
```

문서 → 복사 → 실행보다 **문서 → 실행 → 복사** 경험을 지향합니다.

### 4.5 API Recipe

API 하나가 아니라 여러 API를 조합해 업무를 자동화하는 방법을 보여줍니다.

예: 거래처 변경 조회 → 변경 여부 확인 → Teams 알림.

또는 Excel → API → 거래처 API → Excel 결과와 같은 흐름입니다.

Recipe는 시민개발자에게 **API를 어떻게 조합하는지**를 알려주는 학습 수단이 됩니다.

### 4.6 API Package

API 하나씩 신청하는 대신 업무 단위로 묶습니다.

```text
[ 거래처 업무 Starter ]
✓ 거래처 기본정보
✓ 거래처 담당자
✓ 거래처 상태
✓ 거래처 변경이력
```

API를 기술적 Endpoint 집합이 아니라 **업무 Capability**로 바라보게 만드는 기능입니다.

### 4.7 API Recommendation

검색 결과 외에도 관련 API를 추천합니다.

초기에는 Tag / Category 기반, 이후 사용 패턴 기반, 장기적으로 LLM 기반 의미 검색으로 발전시키는 것이 현실적입니다.

### 4.8 Power Platform 연계

시민개발자라는 목적을 고려하면 중요한 확장점입니다. Azure API Management는 API를 Power Platform Custom Connector 형태로 연결해 Power Apps, Power Automate, Copilot Studio 등에서 사용할 수 있도록 지원합니다.

따라서 Portal에서도 API 상세에 `[ Power Automate로 사용 ]` 같은 진입점을 둘 수 있습니다.

### 4.9 API Quality / Health

별점 대신 객관적인 지표를 제공합니다.

```text
API 상태       ● 정상
최근 24h 호출   12,482
성공률         99.94%
P95 Latency    182ms
최근 업데이트   2026-09-20
Version        v1
```

또한 API 장애나 지연 상태를 보여주는 Health / Status 화면을 제공하면 운영 문의도 줄일 수 있습니다.

### 4.10 API Change Impact

API 변경 시 영향을 받는 소비자를 보여줍니다.

```text
API v1 변경
영향받는 Application 7
사용 중인 PAT        18
Power Automate Flow   5
```

현재 Application / PAT / Usage 구조와 자연스럽게 연결되는 장기 핵심 기능입니다.

### 4.11 Deprecation Center

Deprecated API의 종료일, 영향받는 Consumer, Migration Guide, 신규 Version 전환을 한곳에서 관리합니다.

### 4.12 My API

시민개발자 개인 Dashboard를 제공합니다.

```text
My API
사용 중인 API 12
Application 4
PAT 3

최근 호출
거래처 API 1,245
구매 API 382
인사 API 91
```

### 4.13 AI / Agent-friendly API Catalog

향후 AI Agent가 API를 발견하고 조합할 수 있도록 Catalog Metadata를 풍부하게 구성합니다.

```text
API
 ├─ Business Meaning
 ├─ OpenAPI
 ├─ Examples
 ├─ Input / Output
 ├─ Permission
 ├─ Scope
 ├─ Usage Policy
 ├─ Version
 ├─ Deprecation
 └─ Recipe
```

사람이 검색하는 Catalog에서 AI Agent가 사용할 수 있는 **Machine-readable Capability Catalog**로 확장할 수 있습니다.

## 5. 시민개발자 API Portal의 목표 모습

```text
                 API Catalog
                      ↓
             Search / Discover
                      ↓
                 Playground
                      ↓
                Recipe / Package
                      ↓
                  Application
                      ↓
                   Approval
                      ↓
                     PAT
                      ↓
                    APISIX
                      ↓
                Enterprise API
                      ↓
             Usage / Health / Lifecycle
```

현재의 Catalog → Application → PAT → APISIX → Usage/Audit 흐름을 유지하면서 자연스럽게 확장할 수 있습니다.

## 6. 기능 우선순위

| 단계 | 기능 | 시민개발자 가치 | 구현 난이도 |
|---|---|---:|---:|
| 1 | API Playground | 매우 높음 | 중 |
| 2 | Business-oriented Search | 매우 높음 | 중 |
| 3 | Business-friendly Description | 높음 | 낮음 |
| 4 | Copy & Run | 높음 | 낮음 |
| 5 | API Recipe | 매우 높음 | 중 |
| 6 | API Package | 높음 | 중 |
| 7 | My API Dashboard | 높음 | 중 |
| 8 | API Health | 높음 | 중 |
| 9 | Change Impact | 매우 높음 | 높음 |
| 10 | Deprecation Center | 높음 | 중 |
| 11 | Power Platform Connector | 매우 높음 | 높음 |
| 12 | AI/Agent Catalog | 장기적으로 매우 높음 | 높음 |

## 7. 가장 추천하는 다음 3개

현재 구현 수준에서는 다음 세 기능을 먼저 묶는 것이 좋습니다.

### ① API Playground
**API를 바로 써본다.**

### ② API Recipe
**API를 어떻게 조합하는지 보여준다.**

### ③ API Package
**업무 단위로 API를 제공한다.**

이 세 기능이 결합되면 Portal의 메시지가 ‘API를 신청하는 곳’에서 **‘업무 자동화를 위해 API를 발견하고, 시험하고, 조합하고, 사용할 수 있는 곳’**으로 바뀝니다.

## 8. 최종 제안

다음 발전 방향은 **Security Platform → Developer Experience Platform**으로 무게중심을 이동시키는 것입니다.

전체 로드맵은 다음과 같이 표현할 수 있습니다.

```text
Discover → Understand → Try → Compose → Subscribe → Use → Monitor → Evolve → Agent
```

즉 시민개발자 API Portal을 단순한 **API Gateway 관리 화면**이 아니라,

> 기업 내부의 API를 업무 Capability로 발견하고, 직접 시험하고, 조합하여 자동화할 수 있는 Self-Service API Platform

으로 발전시키는 방향을 권고합니다.

## 9. 참고 사례

- SAP Business Accelerator Hub: API Discover / Explore / Test
- Kong Konnect Dev Portal: API Catalog / Self-Service / API Package
- Microsoft Azure API Management: Power Platform Custom Connector 연계
- Apigee: API Product / Subscription / Developer Portal

## 10. 권장 Portal 메뉴 구조

```text
홈
├─ API 찾기
│   ├─ 전체 API
│   ├─ 업무별
│   ├─ 인기 API
│   └─ 추천 API
│
├─ API 사용하기
│   ├─ Playground
│   ├─ Recipe
│   └─ Package
│
├─ 나의 API
│   ├─ Applications
│   ├─ PAT
│   ├─ Usage
│   └─ API 권한
│
└─ API 운영
    ├─ Health
    ├─ Version
    ├─ Deprecation
    └─ Change Impact
```