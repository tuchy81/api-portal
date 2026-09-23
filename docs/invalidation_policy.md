# PAT · JWT 캐시 무효화 정책

- 문서 ID: SA-OP-CDP-004
- 상위 문서: SA-ARCH-CDP-001 §4.3(핵심 설계 원칙), SA-SPEC-CDP-002 §2.2(Redis 키)
- 목적: 권한·상태 변경이 있을 때 **어떤 이벤트가 즉시 반영되고, 어떤 이벤트는 최대 얼마나 지연되는지** 명시. 운영·감사 시 캐시 잔존에 대한 판단 근거로 사용.

---

## 1. 반영 시점 매트릭스

| 이벤트 | 반영 시점 | 처리 방법 | 코드 위치 |
| :--- | :--- | :--- | :--- |
| PAT 폐기 (사용자·관리자) | **즉시** | `invalidate_pat` — Redis PAT·JWT 캐시 동시 삭제 | `services/portal-backend/routers/tokens.py:revoke_pat` |
| PAT 쿼터 변경 | **즉시** | `update_pat_quota_in_redis` — Redis PAT hash 필드 갱신 | `services/portal-backend/routers/tokens.py:update_quota` |
| API 카탈로그 status 변경 (PUBLISHED ↔ DRAFT/RETIRED) | **즉시** | `_tokens_for_api` 로 관련 PAT 조회 → `invalidate_jwt_cache` | `services/portal-backend/routers/catalog.py:patch_api` |
| API scope 재정의 (append/remove/rename) | **즉시** | 위와 동일 | 위와 동일 |
| API upstream_url · public_path 변경 | **즉시** | 위와 동일 (라우트 재등록과 동반) | 위와 동일 |
| 사용자 Disable / Delete (Keycloak) | **즉시** (webhook 수신 시) | `/internal/user-events` 가 해당 user_sub 의 ACTIVE PAT 전량 REVOKED 처리 + `invalidate_pat` | `services/portal-backend/routers/internal.py:receive_user_event` |
| 사용자 Logout (Keycloak 세션) | **즉시** | 위 엔드포인트에서 JWT 캐시만 flush (PAT 유지) | 위와 동일 |
| Keycloak Role 변경 (신규 부여/회수) | **최대 240초** | JWT Cache TTL 만료 후 다음 호출에서 재교환 | `services/token-exchange-service/config.py:jwt_cache_ttl` |
| PDP 정책 변경 | 즉시 | Internal Gateway 뒤에서 매 요청마다 PDP 재질의 | (Citizen zone 밖) |

---

## 2. 즉시 반영 규칙의 근거

**"즉시" 로 분류된 항목의 공통 원리**: PAT 원장(cdp.pat) 상태와 게이트웨이가 보는 캐시 사이의 불일치 시간을 0으로 만들기 위해, DB 갱신 트랜잭션이 커밋된 직후 같은 요청 안에서 Redis 캐시를 명시적으로 제거합니다.

- **PAT 캐시 삭제(`invalidate_pat`)**: `cdp:pat:{tokenId}`, `cdp:pat:neg:{tokenId}`, `cdp:jwtidx:{tokenId}` 집합의 원소들(`cdp:jwt:{tokenId}:*`), `cdp:jwtidx:{tokenId}` 자체를 모두 삭제. 다음 요청은 fallback 경로를 타고 원장을 다시 읽음 — 이때 status가 REVOKED이면 401.
- **JWT 캐시만 삭제(`invalidate_jwt_cache`)**: PAT는 여전히 유효하지만 새 scope/claim 을 얻기 위해 Token Exchange를 다시 돌게 만듭니다. `cdp:pat:*` 는 건드리지 않으므로 인증 자체는 재캐시 없이 통과.

---

## 3. TTL 기반 반영 규칙의 근거

**"최대 240초"** 는 JWT Cache TTL (`settings.jwt_cache_ttl`) 값입니다. Keycloak Role 처럼 즉시 반영을 위한 명시적 훅이 없는 이벤트는 이 TTL 안에서 자연스럽게 반영됩니다.

TTL을 낮추면 반영 지연이 줄어드는 대신 Keycloak Token Exchange 호출 빈도가 증가하고, TTL을 높이면 반대가 됩니다. 240초는 §4.2 (아키텍처 정의서) 성능 목표(P95 ≤ 50ms, 캐시 적중률 ≥ 95%) 와 부합하는 값이므로 정책 기본치로 유지합니다.

**"240초 안에 반드시 반영되어야 하는" 이벤트가 새로 생기면**:
- 즉시성이 필요하면 §2 방식으로 훅을 추가 (권장)
- 즉시성이 필요 없으면 정책 표에만 추가하고 TTL 만료로 반영

---

## 4. Keycloak Event Webhook

### 4.1 엔드포인트

```
POST /internal/user-events
Headers: X-Internal-Key: <shared-secret>
Body:
{
  "userSub": "u-10233",
  "eventType": "DISABLE" | "DELETE" | "LOGOUT",
  "reason": "부서이동 (선택)"
}
```

### 4.2 동작

| eventType | PAT 상태 변경 | Redis 조작 | 감사 로그 |
| :--- | :--- | :--- | :--- |
| `DISABLE` | `ACTIVE → REVOKED` (전량) | `invalidate_pat` 각 tokenId | `PAT_REVOKED` (trigger=keycloak) |
| `DELETE` | 동일 | 동일 | 동일 |
| `LOGOUT` | 변경 없음 | `invalidate_jwt_cache` 만 | 없음 |

동일 사용자에 대한 중복 호출은 idempotent (남아있는 ACTIVE PAT 만 처리).

### 4.3 Keycloak 측 연동

Keycloak SPI(Service Provider Interface) `EventListenerProvider` 또는 Admin Event Listener 를 통해 아래 이벤트를 웹훅으로 전달하도록 구성:

- `USER_DISABLED`, `DELETE_USER` → `eventType: DISABLE`/`DELETE`
- `LOGOUT`, `LOGOUT_ERROR` → `eventType: LOGOUT`

Keycloak SPI 커스터마이징이 어려운 경우, 배치 접근 (인사시스템 연동 시 daily batch) 을 임시 대체로 사용하고 TTL 만료를 함께 보완책으로 삼습니다.

---

## 5. 운영 체크리스트

- [ ] Portal 배포 시 `SERVER_KEY`, `INTERNAL_API_KEY` 확인
- [ ] Keycloak 이벤트 리스너 등록 여부 확인
- [ ] Redis Sentinel/Cluster 장애 시 캐시 미스 fallback 이 Fail-Closed로 동작하는지 정기 훈련
- [ ] JWT Cache TTL 이 실제 서비스 정책과 일치하는지 릴리즈 노트에서 검토

---

## 6. 관련 파일

- `services/portal-backend/redis_client.py` — `invalidate_pat`, `invalidate_jwt_cache`
- `services/portal-backend/routers/tokens.py` — PAT 폐기·쿼터 변경 훅
- `services/portal-backend/routers/catalog.py` — 카탈로그 변경 훅
- `services/portal-backend/routers/internal.py` — Keycloak 웹훅 엔드포인트
- `services/token-exchange-service/cache.py` — JWT Cache TTL 소스
