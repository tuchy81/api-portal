# Citizen Gateway → Internal Gateway 헤더 신뢰경계

- 문서 ID: SA-OP-CDP-003
- 상위 문서: SA-ARCH-CDP-001 §8, SA-SPEC-CDP-002 §6.2
- 목적: 시민개발자 게이트웨이(APISIX)가 다운스트림으로 넘기는 HTTP 헤더를 **어디까지 신뢰할 수 있는지** 명시 계약으로 남김. 도메인 서비스 개발자가 인가 판단을 잘못 걸치지 않도록 하기 위함.

---

## 1. 헤더 유형 정의

| 등급 | 의미 | 도메인 서비스가 인가 판단에 사용해도 되는가 |
| :--- | :--- | :--- |
| **Authority** (권위 있는 청구) | JWT 서명 검증 후 획득한 claim 또는 Internal Gateway가 발급한 신뢰 헤더 | ✅ 가능 |
| **Context** (문맥 정보) | 편의를 위해 Gateway가 주입한 파생 헤더 | ❌ 사용 금지, 참고용 |

핵심 원칙:

> **Citizen Gateway는 PAT를 이해하는 마지막 지점, Internal Gateway는 검증된 JWT를 신뢰하는 첫 번째 지점.**
> 도메인 서비스는 반드시 **Internal Gateway 통과 후 JWT의 검증된 claim** 을 authority로 사용하고, Gateway가 주입한 편의 헤더는 로깅·힌트로만 참고한다.

---

## 2. Citizen Gateway가 주입하는 헤더

`services/apisix-gateway/plugins/apisix/plugins/pat-token-exchange.lua` 의 `CLAIM_HEADER_MAP` 및 부수 헤더를 기준으로 합니다.

### 2.1 신뢰 헤더 (Gateway가 재작성 · 재주입)

| 헤더 | 출처 | 등급 | 비고 |
| :--- | :--- | :--- | :--- |
| `Authorization: Bearer <JWT>` | TXS가 발급한 교환 JWT | Authority (단, Internal Gateway가 재검증) | Citizen Gateway 도달 시점의 원본 `Authorization: Bearer <PAT>` 는 폐기 |
| `X-Citizen-PAT-Id` | pat-auth가 파싱한 tokenId | Context | 도메인 서비스는 인가 판단에 사용 금지. 감사·트레이싱용 |
| `X-Citizen-Channel: citizen` | Gateway 고정값 | Context | 시민개발자 채널 식별 |
| `X-Request-Id` | Gateway 생성 trace_id | Context | Correlation ID |

### 2.2 JWT payload 파생 헤더 (편의 목적)

`pat-token-exchange.lua` 는 JWT payload를 **서명 검증 없이** decode하여 아래 헤더를 주입합니다.

| 헤더 | JWT claim | 용도 |
| :--- | :--- | :--- |
| `X-USER-ID` | `user_id` | 도메인 서비스가 사용자 식별을 편하게 하기 위한 힌트 |
| `X-COMPANY` | `company` | 회사(계열사) 코드 |
| `X-ORG-CD` | `org_cd` | 조직 코드 |
| `X-ASGN-CD` | `asgn_cd` | 발령 코드 |
| `X-DEPT-CD` | `dept_cd` | 부서 코드 |
| `X-JOB-TIT-CD` | `job_tit_cd` | 직책 코드 |
| `X-OFFI-RES-CD` | `offi_res_cd` | 직위 코드 |
| `X-USER-ORIGIN` | `user_origin` | 사용자 유형 |

**등급 = Context**. 다음이 성립하기 전까지 도메인 서비스는 이 헤더로 인가 결정을 내리면 안 됩니다:
- Internal Gateway가 JWT 서명·issuer·audience·expiry를 검증
- Internal Gateway 또는 PDP가 검증된 claim으로부터 신뢰 헤더를 재발급

즉 **이 헤더의 값을 신뢰해도 되는 조건은 "Internal Gateway를 통과했다"** 이지 "헤더가 존재한다" 가 아닙니다.

### 2.3 클라이언트로부터 제거되는 헤더 (Sanitization)

`strip_spoofable_headers` 가 매 요청에서 아래 헤더를 제거합니다. 클라이언트가 미리 넣어도 서비스는 볼 수 없습니다.

- `X-Citizen-*` 계열 전체
- `X-Forwarded-User`, `X-User-Sub`
- `Cookie`
- §2.2에 나열된 `X-USER-ID`, `X-COMPANY`, `X-ORG-CD`, `X-ASGN-CD`, `X-DEPT-CD`, `X-JOB-TIT-CD`, `X-OFFI-RES-CD`, `X-USER-ORIGIN`

---

## 3. 도메인 서비스가 지켜야 할 규칙

1. **인가 결정은 Authority 헤더/Claim 에서만 도출한다**
   - JWT payload를 서비스에서 파싱해도 되지만, `Bearer <JWT>` 의 서명·issuer·audience·expiry 검증을 이미 통과한 뒤에만 사용 (Internal Gateway 뒤 위치 필수).
   - Citizen Gateway가 주입한 `X-USER-ID` 등을 직접 인가 조건으로 사용 금지.

2. **불일치 시 JWT claim이 우선**
   - 편의상 `X-USER-ID` 를 참조하더라도, 동일 요청의 JWT `sub` 또는 `user_id` claim과 다르면 JWT를 신뢰하고 헤더는 로깅 대상으로만 처리.

3. **감사 로그에는 Gateway 헤더도 함께 기록**
   - `X-Citizen-PAT-Id`, `X-Request-Id`, `X-Citizen-Channel` 은 사고 조사 시 PAT 단위 상관관계 추적에 필수. 인가에는 사용하지 않되 로그에는 남긴다.

---

## 4. 장기 목표 (재작업 예정)

현재는 Citizen Gateway가 편의 헤더를 주입하지만, **최종 목표**는 다음과 같습니다:

1. Citizen Gateway는 `Authorization: Bearer <JWT>` + `X-Citizen-*` 만 주입 (§2.1 신뢰 헤더).
2. §2.2의 파생 헤더 주입 로직은 Citizen Gateway에서 제거.
3. Internal Gateway가 JWT 서명 검증 후 필요 헤더를 표준 스키마로 재발급.

이 재구성이 완료되면 도메인 서비스가 § 2.2 헤더를 참조할 수 있는 지점은 오직 Internal Gateway 뒤로 한정되어, "헤더가 존재한다" 와 "헤더가 authority 다" 가 동치가 됩니다. 완료 전까지는 도메인 서비스가 헤더에 의존하지 않도록 코드 리뷰에서 확인 필요.

---

## 5. 관련 파일

- `services/apisix-gateway/plugins/apisix/plugins/pat-token-exchange.lua` — 헤더 주입/스트립 구현
- `services/apisix-gateway/plugins/apisix/plugins/pat-auth.lua` — 요청 초기 sanitization + trace_id 생성
- `01_시민개발자_API포털_아키텍처정의서.md` §8.1 (STRIDE - Tampering 대응)
- `02_시민개발자_API포털_구현상세스펙.md` §6.2 (프록시 헤더 리스트)
