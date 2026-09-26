# Token Exchange Service (TXS) 코드 분석 문서

> `services/token-exchange-service` 전체 소스를 읽고 분석한 결과입니다.
> **원본 코드는 수정하지 않았으며**, 아래 코드 블록은 분석용으로 한글 주석을 덧붙인 사본입니다.

## 이 서비스는 무엇을 하는가

Citizen Developer API Portal의 **PAT(Personal Access Token) → 내부 시스템용 JWT 교환기**입니다. APISIX 게이트웨이의 `pat-token-exchange.lua` 플러그인이 PAT 검증을 마친 뒤 이 서비스를 호출하면, TXS는 Keycloak과 **RFC 8693 Token Exchange**를 수행해 해당 시민 개발자(user_sub)를 impersonate하는 내부 JWT를 발급/캐싱해서 돌려줍니다. 즉 게이트웨이-Keycloak 사이의 중개자이자, JWT 발급 비용(네트워크 왕복)을 Redis 캐시로 줄여주는 캐싱 레이어입니다.

## 파일 목록과 역할

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI 앱 진입점. `/internal/token-exchange`, `/health`, `/metrics` 엔드포인트 |
| `config.py` | pydantic-settings 기반 환경설정(Keycloak, Redis, 캐시 TTL, 서킷브레이커 파라미터 등) |
| `caller_auth.py` | 호출자 인증(공유키 + CIDR 허용목록) — 게이트웨이만 호출 가능하도록 제한 |
| `cache.py` | JWT Redis 캐시 + single-flight 락 + 확률적 조기 갱신(probabilistic early expiration) |
| `keycloak_client.py` | Keycloak과의 실제 OAuth2/RFC 8693 HTTP 통신 |
| `circuit_breaker.py` | Redis 기반 서킷 브레이커(연속 실패율 감시) |
| `redis_client.py` | Redis 커넥션 풀 관리 + Lua 스크립트 사전 로드 |
| `metrics.py` | Prometheus 커스텀 메트릭 정의 |
| `Dockerfile` / `requirements.txt` | 컨테이너 빌드 정의, 의존 라이브러리 |

### 요청 처리 흐름 (`/internal/token-exchange`)

```
게이트웨이(pat-token-exchange.lua)
      │  POST /internal/token-exchange {tokenId, userSub, scopes}
      ▼
caller_auth.check_caller()        ── X-Internal-Key 대조 + CIDR 허용목록
      │ 통과
      ▼
circuit_breaker.is_circuit_open() ── OPEN이면 캐시된 JWT라도 있으면 반환, 없으면 503
      │ CLOSED/HALF_OPEN
      ▼
cache.get_or_exchange_jwt()
      ├─ Redis 캐시 HIT → (probabilistic하게) 백그라운드 갱신 트리거 후 즉시 반환
      └─ 캐시 MISS → Redis 락(single-flight) 획득
            ├─ 획득 성공 → keycloak_client.exchange_token() 호출(RFC 8693) → 캐시 저장 → 락 해제
            └─ 획득 실패(다른 요청이 이미 교환 중) → 짧게 폴링하며 캐시가 채워지길 대기, 타임아웃 시 에러
      ▼
circuit_breaker.record_success()/record_failure() ── 성공/실패를 슬라이딩 윈도우에 기록
      ▼
Prometheus 메트릭 기록 후 ExchangeResponse 반환
```

---

## 1. `main.py` — FastAPI 진입점

```python
"""Token Exchange Service — RFC 8693, JWT cache, circuit breaker."""
import time, logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

import redis_client as rc
import circuit_breaker as cb_module
import cache as jwt_cache
import caller_auth
import metrics as m
from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("txs")

app = FastAPI(title="Token Exchange Service")

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    r = rc.get_redis()
    rc.load_lua_scripts(r)                 # unlock 스크립트를 Redis에 미리 SCRIPT LOAD (아래 7절 참고 — 실제로는 사용되지 않음)
    caller_auth.warn_if_unprotected()      # INTERNAL_API_KEY 미설정 시 경고 로그
    logger.info("TXS started. Redis connected.")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
# 게이트웨이(pat-token-exchange.lua)가 보내는 요청 바디와 정확히 대응됨
# (Lua 쪽: cjson.encode({ tokenId = ..., userSub = ..., scopes = ... }))
class ExchangeRequest(BaseModel):
    tokenId: str
    userSub: str
    scopes: list[str]

class ExchangeResponse(BaseModel):
    accessToken: str
    expiresIn: int
    jti: str        # JWT의 jti 클레임 — 게이트웨이 pat-audit.lua가 감사 로그 상관관계 용도로 재추출
    cached: bool     # 캐시 HIT으로 응답했는지 여부 (관측/디버깅용)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/internal/token-exchange", response_model=ExchangeResponse)
async def token_exchange(req: ExchangeRequest, request: Request):
    # 1) 호출자 검증 — 게이트웨이가 아닌 호출자는 여기서 403으로 차단
    denied = caller_auth.check_caller(request)
    if denied:
        return denied

    r = rc.get_redis()

    # 2) 서킷 브레이커가 열려 있으면(Keycloak이 계속 실패 중이라고 판단되면)
    #    새로 교환을 시도하지 않고, "혹시 캐시에 아직 살아있는 JWT가 있는지"만 확인.
    #    같은 scope 조합에 대해 이전에 발급받은 JWT가 TTL 내라면 그걸로 응답을 계속 내려줄 수 있다
    #    (완전 장애 시에도 이미 발급된 토큰으로는 서비스 지속 가능하게 하는 degrade 전략).
    if cb_module.is_circuit_open(r):
        import hashlib
        # 주의: cache.py의 _scope_hash와 동일한 로직을 여기서 다시 구현하고 있다
        # (import 재사용이 아니라 인라인 중복 — cache._scope_hash를 불러써도 될 부분).
        sh = hashlib.sha256(" ".join(sorted(req.scopes)).encode()).hexdigest()[:8]
        cached_jwt = r.get(f"cdp:jwt:{req.tokenId}:{sh}")
        if cached_jwt:
            from keycloak_client import _extract_jti
            m.token_exchange_total.labels(result="hit").inc()
            return ExchangeResponse(
                accessToken=cached_jwt,
                expiresIn=settings.jwt_cache_ttl,
                jti=_extract_jti(cached_jwt),
                cached=True,
            )
        # 캐시도 없으면 정말 아무것도 해줄 수 없으므로 503 (RFC 9457 형식)
        return JSONResponse(
            {"type": "https://cdp-portal.hd.com/errors/CDP-2001",
             "title": "Authorization service unavailable",
             "status": 503, "code": "CDP-2001",
             "detail": "Circuit breaker is OPEN and no cached JWT available"},
            status_code=503
        )

    # 3) 정상 경로: 캐시 조회 또는 Keycloak과 실제 토큰 교환 수행
    start = time.perf_counter()
    try:
        jwt_str, was_cached = await jwt_cache.get_or_exchange_jwt(
            req.tokenId, req.userSub, req.scopes, r, settings.node_id
        )
        elapsed = time.perf_counter() - start
        m.token_exchange_latency.observe(elapsed)
        m.token_exchange_total.labels(result="hit" if was_cached else "miss").inc()
        cb_module.record_success(r)   # 성공을 서킷 브레이커 슬라이딩 윈도우에 기록

        from keycloak_client import _extract_jti
        return ExchangeResponse(
            accessToken=jwt_str,
            # 캐시 HIT이면 "남은 캐시 TTL 기준"이 아니라 캐시 TTL 설정값을 그대로 보고하고,
            # 새로 발급했으면(MISS) 실제 JWT 수명(jwt_ttl)을 보고한다.
            expiresIn=settings.jwt_ttl if not was_cached else settings.jwt_cache_ttl,
            jti=_extract_jti(jwt_str),
            cached=was_cached,
        )
    except TimeoutError as e:
        # single-flight 대기(cache.py)가 시간 내에 캐시를 채우지 못한 경우
        m.token_exchange_total.labels(result="fail").inc()
        cb_module.record_failure(r)
        return JSONResponse(
            {"type": "https://cdp-portal.hd.com/errors/CDP-2001",
             "title": "Token exchange timeout",
             "status": 503, "code": "CDP-2001", "detail": str(e)},
            status_code=503
        )
    except Exception as e:
        # Keycloak 호출 실패 등 그 외 모든 예외 — 실패로 기록하고 503 반환
        logger.error(f"Token exchange error: {e}")
        m.token_exchange_total.labels(result="fail").inc()
        cb_module.record_failure(r)
        return JSONResponse(
            {"type": "https://cdp-portal.hd.com/errors/CDP-2001",
             "title": "Authorization service unavailable",
             "status": 503, "code": "CDP-2001", "detail": str(e)},
            status_code=503
        )

@app.get("/health")
def health():
    # 서킷 브레이커 현재 상태까지 함께 노출해, 헬스체크만 봐도 "정상인데 Keycloak이
    # 불안정한 상태"인지 구분할 수 있게 한다.
    r = rc.get_redis()
    circuit_state = r.get("cdp:cb:txs") or "CLOSED"
    return {"status": "ok", "service": "token-exchange-service", "circuit": circuit_state}

@app.get("/metrics")
def metrics():
    # Prometheus가 스크레이핑하는 표준 엔드포인트
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

**핵심 포인트**
- 요청 1건당 항상 **호출자 인증 → 서킷 브레이커 확인 → 캐시/교환 → 결과 기록** 순으로 처리한다.
- 서킷 브레이커가 열려 있어도 "캐시에 아직 유효한 JWT가 있으면 계속 응답"하는 **그레이스풀 디그레이드(graceful degrade)** 전략을 쓴다.
- 모든 실패 응답이 RFC 9457(`problem+json` 스타일) 형식으로 통일되어 있어, 게이트웨이(`common.problem_json`)와 응답 규약이 일치한다.

---

## 2. `config.py` — 환경설정

```python
import secrets
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    # 2026-09-26 이후 로컬 컴포즈에서는 `KEYCLOAK_URL=http://keycloak:8080` (실 Keycloak 24.0.0)이
    # 주입되어 이 기본값을 덮어씁니다. `mock-keycloak:8180` 기본값은 컴포즈 환경 변수 없이 단독으로
    # TXS를 돌릴 때의 안전망으로만 남아있고 실제 실행 경로에서는 사용되지 않습니다.
    keycloak_url: str = "http://mock-keycloak:8180"
    keycloak_realm: str = "hd"
    exchanger_client_id: str = "citizen-gw-exchanger"        # TXS가 Keycloak에 자신을 인증하는 서비스 계정
    exchanger_client_secret: str = "exchanger-secret-xyz"
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = "changeme123"
    jwt_cache_ttl: int = 240     # Redis에 캐싱해두는 JWT의 TTL(초) — 실제 JWT 수명보다 짧게 잡아 여유를 둠
    jwt_ttl: int = 300           # Keycloak이 실제로 발급하는 JWT의 수명(초)
    cb_failure_threshold: float = 0.5   # 최근 윈도우에서 실패율이 이 값 이상이면 서킷을 OPEN
    cb_window_size: int = 50            # 서킷 브레이커가 관찰하는 최근 호출 개수
    cb_open_duration: int = 30          # OPEN 상태 유지 시간(초) — 이후 자동으로 다시 판단
    retry_max_attempts: int = 2         # (선언만 되어 있고 현재 코드에서 실제 재시도 로직에 사용되진 않음 — 아래 관찰 참고)
    retry_wait_ms: int = 200
    request_timeout_s: float = 3.0      # Keycloak 호출 타임아웃
    # 이 TXS 인스턴스(프로세스)를 구분하는 노드ID. 프로세스 시작 시 1회 랜덤 생성되며,
    # single-flight 락의 "소유자 값"으로 쓰여 - 내가 건 락만 내가 해제하도록 보장한다.
    node_id: str = Field(default_factory=lambda: secrets.token_hex(8))
    # Caller authentication for /internal/token-exchange. The shared secret is
    # the primary control; allowed_cidr is the spec-mandated IP allowlist but is
    # weak on its own in Docker/K8s where caller IPs are dynamic.
    internal_api_key: str = ""
    allowed_cidr: str = "0.0.0.0/0"

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
```

**핵심 포인트**
- `node_id`는 프로세스마다 고유한 랜덤 값으로, Redis 락(`cache.py`)에서 "이 락을 누가 걸었는지"를 표시하는 토큰으로 쓰인다.
- `jwt_cache_ttl`(240초)이 `jwt_ttl`(300초)보다 짧게 설정되어 있는 점에 주목 — 캐시가 실제 JWT 만료보다 먼저 비워지도록(60초 여유) 설계해, **만료된 JWT를 캐시에서 내려주는 상황을 방지**한다.
- `retry_max_attempts`/`retry_wait_ms`는 선언되어 있지만 `keycloak_client.py`의 실제 HTTP 호출 코드에는 재시도 루프가 없다(→ 아래 "관찰" 섹션 참고). `requirements.txt`에는 `tenacity`(재시도 라이브러리)가 포함되어 있어, 원래 재시도 데코레이터를 적용할 의도였던 것으로 보인다.

---

## 3. `caller_auth.py` — 호출자 인증 (게이트웨이 전용 접근 제어)

```python
"""Caller authentication for TXS internal endpoints (spec 5.1).

Anyone who can reach this service can mint an impersonation JWT for an
arbitrary user by supplying {tokenId, userSub, scopes} — the gateway's PAT
verification happens upstream and is not re-checked here. So the endpoint
must only accept calls from the gateway.

Two layers:
  1. X-Internal-Key shared secret — the primary control.
  2. allowed_cidr IP allowlist — required by spec 5.1, but weak on its own
     since container IPs are dynamic in Docker/K8s; kept as defense in depth.
"""
import hmac
import ipaddress
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from config import settings

logger = logging.getLogger("txs.caller_auth")


def _denied(detail: str) -> JSONResponse:
    # 인가 거부 시 공통으로 쓰는 RFC 9457 형식 403 응답
    return JSONResponse(
        {"type": "https://cdp-portal.hd.com/errors/CDP-1005",
         "title": "Access denied by policy",
         "status": 403, "code": "CDP-1005", "detail": detail},
        status_code=403,
    )


def _client_ip(request: Request) -> str:
    # X-Forwarded-For의 첫 값(원 클라이언트) 우선, 없으면 TCP 연결의 실제 IP
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else ""


def _ip_allowed(client_ip: str) -> bool:
    networks = [c.strip() for c in settings.allowed_cidr.split(",") if c.strip()]
    if not networks:
        return True
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for cidr in networks:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            logger.warning(f"Ignoring malformed CIDR in allowed_cidr: {cidr}")
    return False


def check_caller(request: Request) -> JSONResponse | None:
    """Return a 403 response if the caller is not authorized, else None."""
    # 1차 통제: 공유 비밀키. 설정되어 있지 않으면(로컬 개발) 이 검사 자체를 건너뜀.
    if settings.internal_api_key:
        provided = request.headers.get("X-Internal-Key", "")
        # hmac.compare_digest로 타이밍 공격 방지 (문자열 == 비교 대신)
        if not hmac.compare_digest(provided, settings.internal_api_key):
            logger.warning(f"Rejected call from {_client_ip(request)}: bad X-Internal-Key")
            return _denied("Invalid or missing X-Internal-Key")

    # 2차 통제(심층 방어): 호출자 IP가 허용 CIDR 안에 있는지
    client_ip = _client_ip(request)
    if not _ip_allowed(client_ip):
        logger.warning(f"Rejected call from {client_ip}: not in allowed_cidr")
        return _denied(f"Client IP {client_ip} is not in the allowed CIDR list")

    return None


def warn_if_unprotected() -> None:
    """Called at startup so an unset key is visible in the logs."""
    # 운영 배포에서 키를 깜빡하고 안 넣는 실수를 로그로 눈에 띄게 경고
    if not settings.internal_api_key:
        logger.warning(
            "INTERNAL_API_KEY is not set — /internal/token-exchange accepts any caller "
            "that passes the IP allowlist. Set it before deploying."
        )
```

**핵심 포인트**
- 이 엔드포인트는 `{tokenId, userSub, scopes}`만 주면 **임의 사용자를 사칭하는 JWT를 발급**해줄 수 있다는 것이 모듈 docstring에 명시된 핵심 위협 모델이다. PAT 검증은 게이트웨이(`pat-auth.lua`)에서 이미 끝났다고 신뢰하고 재검증하지 않기 때문에, "게이트웨이만 호출 가능"하도록 강제하는 것이 이 모듈의 유일한 존재 이유다.
- 공유키(`X-Internal-Key`)가 **주 통제**, CIDR 허용목록은 **심층 방어(defense in depth)**로 명확히 구분되어 있으며, 이는 컨테이너 환경에서 IP가 동적이라 CIDR만으로는 신뢰할 수 없기 때문이라고 주석에 설명되어 있다.

---

## 4. `cache.py` — JWT 캐시 + single-flight 락 + 확률적 조기 갱신

```python
"""JWT cache with single-flight and probabilistic early expiration."""
import asyncio, hashlib, random, time, logging
import redis as redis_lib
from config import settings
import keycloak_client
import circuit_breaker as cb_module

logger = logging.getLogger("txs.cache")

# main.py의 startup에서 redis_client.load_lua_scripts()가 같은 내용을 SCRIPT LOAD 해두지만,
# 이 파일은 그 SHA를 쓰지 않고 스크립트 원문을 직접 EVAL한다 (7절 "관찰" 참고).
UNLOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
"""

def _scope_hash(scopes: list[str]) -> str:
    # 게이트웨이 pat-token-exchange.lua의 scope_hash()와 완전히 동일한 로직
    # (정렬 후 공백으로 join → sha256 → 앞 8자). 두 쪽이 같은 캐시 키를 계산해야
    # 게이트웨이가 조회하는 캐시와 TXS가 쓰는 캐시가 같은 키를 가리킨다.
    return hashlib.sha256(" ".join(sorted(scopes)).encode()).hexdigest()[:8]

def _cache_key(token_id: str, scope_hash: str) -> str:
    return f"cdp:jwt:{token_id}:{scope_hash}"

def _lock_key(token_id: str, scope_hash: str) -> str:
    return f"cdp:lock:jwt:{token_id}:{scope_hash}"

def _idx_key(token_id: str) -> str:
    # 이 토큰ID가 발급받은 모든 (scope 조합별) 캐시 키를 모아두는 Set.
    # PAT가 폐기(revoke)될 때 이 인덱스를 순회하며 관련 JWT 캐시를 한 번에 지우는 용도로 보인다
    # (다만 이 파일 자체에는 그 삭제 로직은 없음 — 다른 서비스(portal-backend)의 폐기 처리에서 사용될 값).
    return f"cdp:jwtidx:{token_id}"

async def get_or_exchange_jwt(
    token_id: str,
    user_sub: str,
    scopes: list[str],
    r: redis_lib.Redis,
    node_id: str,
) -> tuple[str, bool]:
    """Returns (jwt_string, was_cached)."""
    sh = _scope_hash(scopes)
    ckey = _cache_key(token_id, sh)
    lkey = _lock_key(token_id, sh)

    # 1. 캐시 조회
    jwt = r.get(ckey)
    if jwt:
        ttl = r.ttl(ckey)
        # Probabilistic early expiration (< 20% of TTL remaining)
        # 캐시가 아직 안 죽었어도, 남은 TTL이 전체 캐시TTL의 20% 미만으로 줄어들면
        # "곧 만료될 테니 미리 갱신해두자"는 확률적 조기 갱신(XFetch류 기법)을 시도한다.
        # TTL이 짧아질수록 갱신을 트리거할 확률 p가 선형으로 1에 가까워진다.
        if ttl > 0 and ttl < settings.jwt_cache_ttl * 0.20:
            p = (settings.jwt_cache_ttl * 0.20 - ttl) / (settings.jwt_cache_ttl * 0.20)
            if random.random() < p:
                logger.debug(f"Probabilistic early refresh for {token_id} (TTL={ttl}s)")
                # 지금 응답은 기존(아직 유효한) JWT로 즉시 내려주고, 갱신은 백그라운드 태스크로 분리.
                # 이렇게 하면 "캐시 만료 직후 몰려드는 요청들이 동시에 Keycloak을 때리는"
                # 캐시 스탬피드(cache stampede)를 요청 다수가 동시에 만료를 감지하기 전에 미리 흩어서 방지한다.
                asyncio.create_task(_refresh_jwt(token_id, user_sub, scopes, r, node_id))
        return jwt, True

    # 2. 캐시 미스 — single-flight: 이 (token_id, scope) 조합에 대해 동시에 여러 요청이
    #    들어와도 딱 하나의 요청만 실제로 Keycloak을 호출하게 하기 위한 락.
    #    SET NX PX 5000 → 5초 안에 못 끝내면 락이 자동 만료(교착 방지).
    lock_acquired = r.set(lkey, node_id, nx=True, px=5000)

    if lock_acquired:
        # 락을 획득한 이 요청만 실제 교환을 수행
        try:
            result = await keycloak_client.exchange_token(token_id, user_sub, scopes)
            jwt = result["access_token"]
            r.setex(ckey, settings.jwt_cache_ttl, jwt)
            r.sadd(_idx_key(token_id), ckey)
            r.expire(_idx_key(token_id), settings.jwt_ttl)
            return jwt, False
        except Exception as e:
            logger.error(f"Token exchange failed for {token_id}: {e}")
            raise
        finally:
            # compare-and-delete: 내가 세팅한 값(node_id)일 때만 지운다.
            # 만약 내 락이 5초 안에 처리를 못 끝내 자동 만료되고 그 사이 다른 노드가
            # 새로 락을 잡았다면, 여기서 무조건 DEL을 했을 경우 그 다른 노드의 락을
            # 실수로 해제해버리는 버그가 생긴다 — 그걸 막기 위한 원자적 스크립트.
            r.eval(UNLOCK_SCRIPT, 1, lkey, node_id)
    else:
        # 락을 못 얻음 = 다른 요청(혹은 다른 TXS 인스턴스)이 이미 교환 중.
        # 그 요청이 캐시를 채워줄 때까지 50ms 간격으로 최대 6번(=300ms) 폴링.
        for _ in range(6):
            await asyncio.sleep(0.05)
            jwt = r.get(ckey)
            if jwt:
                return jwt, True
        # 300ms 안에도 안 채워지면 포기하고 타임아웃 에러 → main.py가 503으로 변환
        raise TimeoutError(f"CDP-2001: Cache not populated after waiting for lock on {token_id}")

async def _refresh_jwt(token_id: str, user_sub: str, scopes: list[str], r: redis_lib.Redis, node_id: str):
    """Background JWT refresh (fire-and-forget)."""
    # 클라이언트 응답과 무관하게 실행되는 백그라운드 갱신. 실패해도 사용자에게 영향 없음
    # (다음 요청이 캐시 미스로 처리되며 정상 경로로 다시 시도됨) — 그래서 예외를 삼키고 warning만 남긴다.
    try:
        sh = _scope_hash(scopes)
        ckey = _cache_key(token_id, sh)
        lkey = _lock_key(token_id, sh)
        if r.set(lkey, node_id, nx=True, px=5000):
            try:
                result = await keycloak_client.exchange_token(token_id, user_sub, scopes)
                r.setex(ckey, settings.jwt_cache_ttl, result["access_token"])
                r.sadd(_idx_key(token_id), ckey)
                r.expire(_idx_key(token_id), settings.jwt_ttl)
            finally:
                r.eval(UNLOCK_SCRIPT, 1, lkey, node_id)
    except Exception as e:
        logger.warning(f"Background refresh failed for {token_id}: {e}")
```

**핵심 포인트**
- **Single-flight 락**: 캐시 미스가 동시에 여러 건 발생해도 Redis `SET NX PX`로 딱 하나의 요청만 Keycloak을 호출하게 하고, 나머지는 짧게 폴링하며 결과를 기다린다. 이는 "썬더링 허드(thundering herd)"로 Keycloak에 부하가 몰리는 것을 막는다.
- **확률적 조기 갱신(probabilistic early expiration, 일명 XFetch 패턴)**: TTL이 얼마 안 남았을 때 일부 요청이 (전부가 아니라 확률적으로) 미리 백그라운드 갱신을 트리거해, "캐시가 정확히 만료되는 순간 요청들이 한꺼번에 몰리는" 캐시 스탬피드를 완화한다.
- **compare-and-delete 락 해제**: 락의 소유권을 `node_id` 값으로 증명하고, `unlock_if_mine.lua`와 동일한 로직(`UNLOCK_SCRIPT`)으로 "내가 건 락일 때만" 해제한다.

---

## 5. `keycloak_client.py` — Keycloak RFC 8693 통신

```python
"""Keycloak token exchange client with service account token caching."""
import time, logging
import httpx
from config import settings

logger = logging.getLogger("txs.keycloak")

# 프로세스 전역(모듈 레벨) 서비스 계정 토큰 캐시. TXS 자신이 Keycloak에 인증하는 데
# 쓰는 client_credentials 토큰을 매 요청마다 새로 받지 않기 위함.
_svc_token_cache: dict = {"token": None, "expires_at": 0}

async def get_exchanger_token() -> str:
    """Get or refresh service account access token (client_credentials)."""
    now = time.time()
    if _svc_token_cache["token"] and now < _svc_token_cache["expires_at"]:
        return _svc_token_cache["token"]

    # OAuth2 client_credentials 그랜트로 TXS 자신의 서비스 계정 토큰 발급
    async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
        resp = await client.post(
            f"{settings.keycloak_url}/realms/{settings.keycloak_realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.exchanger_client_id,
                "client_secret": settings.exchanger_client_secret,
                "scope": "openid",
            },
        )
        resp.raise_for_status()   # 4xx/5xx면 여기서 httpx.HTTPStatusError 발생 → 상위에서 서킷 브레이커 실패로 기록됨
        data = resp.json()
        _svc_token_cache["token"] = data["access_token"]
        # 만료 50초 전에 미리 갱신 대상으로 취급 (경계 시점에 만료된 토큰을 쓰는 것을 방지)
        _svc_token_cache["expires_at"] = now + data.get("expires_in", 300) - 50
        return _svc_token_cache["token"]

async def exchange_token(token_id: str, user_sub: str, scopes: list[str]) -> dict:
    """RFC 8693 token exchange: impersonate user_sub with given scopes."""
    svc_token = await get_exchanger_token()
    scope_str = " ".join(scopes)

    async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
        resp = await client.post(
            f"{settings.keycloak_url}/realms/{settings.keycloak_realm}/protocol/openid-connect/token",
            # 클라이언트 자신도 client_id/secret으로 인증(Basic Auth)하면서,
            auth=(settings.exchanger_client_id, settings.exchanger_client_secret),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",  # RFC 8693 표준 grant_type
                "subject_token": svc_token,                     # "교환의 주체가 되는" 토큰 = 서비스 계정 토큰
                "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "requested_subject": user_sub,                  # 이 사용자로 impersonate 요청
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                # 2026-09-26 실 Keycloak(24.0.0) 전환 후 제거: `--features=token-exchange`가 활성화된
                # 실 Keycloak은 audience가 명시되면 대상 클라이언트의 fine-grained token-exchange 권한
                # 정책을 강제한다. dev realm에는 그 정책이 설정돼 있지 않아 `Client not allowed to exchange`
                # 로 거부되었다. 대신 `citizen-gw-exchanger`의 audience protocol mapper가 발급되는
                # access token의 `aud`에 `internal-api-gateway`를 그대로 주입하도록 realm-import에 넣었다.
                # "audience": "internal-api-gateway",
                "scope": scope_str,                              # PAT에 부여된 scope 그대로 전달
                "cdp_pat_id": token_id,                          # Keycloak 쪽에서 감사/매핑용으로 남길 수 있는 커스텀 클레임 힌트
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "expires_in": data.get("expires_in", settings.jwt_ttl),
            "jti": _extract_jti(data["access_token"]),
        }

def _extract_jti(token: str) -> str:
    # 발급된 JWT에서 jti 클레임만 뽑아본다. 서명 검증은 하지 않는다(get_unverified_claims) —
    # 이 토큰은 방금 신뢰하는 Keycloak으로부터 직접 받은 것이므로 재검증할 필요가 없고,
    # 여기서는 응답 바디/로그 상관관계용으로 jti 값만 필요하기 때문.
    from jose import jwt as jose_jwt
    try:
        return jose_jwt.get_unverified_claims(token).get("jti", "unknown")
    except Exception:
        return "unknown"
```

**핵심 포인트**
- **2단계 OAuth2 흐름**: ① `client_credentials`로 TXS 자신의 서비스 계정 토큰을 얻고(`get_exchanger_token`, 캐싱됨), ② 그 토큰을 `subject_token`으로 삼아 RFC 8693 `token-exchange` 그랜트를 호출해 `requested_subject`(citizen 사용자)로 impersonate된 JWT를 받는다.
- 서비스 계정 토큰 자체도 모듈 레벨 딕셔너리(`_svc_token_cache`)에 캐싱되어, 매 사용자 요청마다 Keycloak에 두 번(서비스 토큰 + 교환) 요청하는 게 아니라 보통은 교환 요청 한 번만 발생한다.
- `cdp_pat_id`를 커스텀 파라미터로 함께 보내는 점으로 보아, Keycloak 프로토콜 매퍼가 이 값을 발급되는 JWT의 클레임으로 심어 하위 시스템에서 "이 요청이 어느 PAT로부터 왔는지" 추적할 수 있게 설계된 것으로 보인다(Keycloak 측 설정은 이 저장소 범위 밖).

---

## 6. `circuit_breaker.py` — Redis 기반 서킷 브레이커

```python
"""Circuit breaker backed by Redis state."""
import time, logging
import redis as redis_lib
from config import settings

logger = logging.getLogger("txs.circuit_breaker")

CB_KEY = "cdp:cb:txs"              # 현재 서킷 상태(OPEN/HALF_OPEN)를 담는 키. 없으면(nil) CLOSED로 간주.
CB_WINDOW_KEY = "cdp:cb:txs:window"  # 최근 호출 결과("OK"/"FAIL")를 담는 슬라이딩 윈도우 리스트

def is_circuit_open(r: redis_lib.Redis) -> bool:
    state = r.get(CB_KEY)
    return state in ("OPEN", "HALF_OPEN")

def record_success(r: redis_lib.Redis):
    # 리스트 맨 앞에 push하고, window_size를 넘는 오래된 항목은 잘라낸다(LTRIM) —
    # 즉 "최근 N개" 슬라이딩 윈도우를 Redis List로 흉내낸 것.
    r.lpush(CB_WINDOW_KEY, "OK")
    r.ltrim(CB_WINDOW_KEY, 0, settings.cb_window_size - 1)
    r.expire(CB_WINDOW_KEY, 300)   # 트래픽이 뚝 끊기면 윈도우가 5분 뒤 자동 소멸(오래된 통계로 영원히 판단하지 않도록)
    _evaluate_window(r)

def record_failure(r: redis_lib.Redis):
    r.lpush(CB_WINDOW_KEY, "FAIL")
    r.ltrim(CB_WINDOW_KEY, 0, settings.cb_window_size - 1)
    r.expire(CB_WINDOW_KEY, 300)
    _evaluate_window(r)

def _evaluate_window(r: redis_lib.Redis):
    window = r.lrange(CB_WINDOW_KEY, 0, -1)
    if len(window) < 10:
        # 표본이 너무 적으면(콜드 스타트 등) 성급하게 서킷을 열지 않는다
        return
    fail_count = sum(1 for x in window if x == "FAIL")
    fail_rate = fail_count / len(window)
    if fail_rate >= settings.cb_failure_threshold:
        # 실패율이 임계치 이상이면 cb_open_duration초 동안 OPEN 상태로 고정.
        # 이 TTL이 지나면 키가 자연 소멸해 is_circuit_open()이 다시 False(CLOSED 취급)를 반환하며
        # 별도의 "HALF_OPEN 프로빙 로직" 없이 사실상 다음 요청이 바로 재시도되는 단순한 형태다.
        r.setex(CB_KEY, settings.cb_open_duration, "OPEN")
        logger.warning(f"Circuit breaker OPEN: failure rate {fail_rate:.0%}")
    else:
        r.delete(CB_KEY)
```

**핵심 포인트**
- 상태 저장을 프로세스 메모리가 아니라 **Redis에 위임**해, TXS가 여러 인스턴스(파드/컨테이너)로 수평 확장돼 있어도 모든 인스턴스가 동일한 서킷 상태를 공유한다(한 인스턴스가 감지한 장애가 즉시 전체 인스턴스에 반영됨).
- `CB_KEY`에 `HALF_OPEN` 문자열도 `is_circuit_open`에서 "열림"으로 취급하도록 준비되어 있지만, `_evaluate_window`가 실제로 쓰는 값은 `"OPEN"`뿐이다 — `HALF_OPEN`으로 전이시키는 코드는 이 파일에 없다(즉 OPEN → TTL 만료 → 즉시 CLOSED로 취급되는 단순화된 2-state 구현이며, 명시적인 half-open 프로빙 단계는 구현되어 있지 않다).
- 최소 표본 수(10개) 미만이면 판단을 유보해, 서비스 시작 직후 몇 번의 실패만으로 서킷이 성급하게 열리는 것을 방지한다.

---

## 7. `redis_client.py` — Redis 커넥션 풀 & 스크립트 로더

```python
import redis as redis_lib
from config import settings
from pathlib import Path

_pool = None
_quota_sha: str = None    # 선언만 되어 있고 이 파일 안에서 채워지거나 사용되는 곳이 없음 (TXS는 쿼터 차감을 하지 않음 — pat-quota.lua/portal-backend의 몫)
_unlock_sha: str = None

def get_redis() -> redis_lib.Redis:
    global _pool
    if _pool is None:
        # 커넥션 풀은 프로세스당 1회만 생성(지연 초기화), 이후 호출은 풀에서 커넥션을 빌려오는 얇은 Redis 클라이언트만 생성
        _pool = redis_lib.ConnectionPool(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            decode_responses=True,   # 응답을 bytes가 아닌 str로 자동 디코딩
            max_connections=20,
        )
    return redis_lib.Redis(connection_pool=_pool)

def load_lua_scripts(r: redis_lib.Redis):
    global _unlock_sha
    # infra/redis/unlock_if_mine.lua를 Redis 서버에 미리 SCRIPT LOAD 해서 SHA를 캐싱해두려는 의도.
    unlock_lua = Path(__file__).parent / "infra/redis/unlock_if_mine.lua"
    if unlock_lua.exists():
        _unlock_sha = r.script_load(unlock_lua.read_text())

def get_unlock_sha() -> str:
    return _unlock_sha
```

**핵심 포인트 / 관찰**
- Redis 클라이언트는 커넥션 풀을 통해서만 생성되며, `get_redis()`를 호출할 때마다 새 `Redis` 객체를 만들지만 내부적으로 같은 풀의 TCP 커넥션을 재사용하므로 매 요청 커넥션 생성 비용은 없다.
- **[관찰] `_unlock_sha`/`get_unlock_sha()`는 사실상 미사용 코드다.** `main.py`의 startup이 `load_lua_scripts()`를 호출해 `unlock_if_mine.lua`를 Redis에 `SCRIPT LOAD`하고 SHA를 저장해두지만, 저장소 전체에서 `get_unlock_sha()`를 호출하는 곳이 없다. 실제 락 해제는 `cache.py`가 자체적으로 갖고 있는 `UNLOCK_SCRIPT` 문자열 상수를 `r.eval(...)`(SHA 캐싱 없는 매번 원문 전송 방식)로 실행한다. 두 구현의 스크립트 내용 자체는 `infra/redis/unlock_if_mine.lua`와 동일하지만, 사전 로드해둔 SHA는 그대로 버려지는 셈이다.
- `_quota_sha` 변수 역시 선언만 되어 있고 아무 곳에서도 대입/조회되지 않는다 — 쿼터 차감(`quota_deduct.lua`)은 이 서비스가 아니라 게이트웨이의 `pat-quota.lua`가 직접 수행하는 영역이라, TXS 쪽에는 애초에 필요 없는 흔적(과거 설계 잔재로 추정)일 가능성이 있다.

---

## 8. `metrics.py` — Prometheus 메트릭 정의

```python
from prometheus_client import Counter, Histogram

token_exchange_total = Counter(
    "cdp_token_exchange_total",
    "Token exchange results",
    ["result"]  # hit / miss / fail   ← main.py가 각 분기에서 .labels(result=...).inc() 호출
)

token_exchange_latency = Histogram(
    "cdp_token_exchange_latency_seconds",
    "Token exchange latency in seconds",
)
```

**핵심 포인트**
- `result` 레이블로 `hit`(캐시 적중) / `miss`(실제 Keycloak 교환 발생) / `fail`(실패) 비율을 구분해, "캐시 적중률"과 "실패율"을 각각 대시보드에서 추적할 수 있게 한다.
- `token_exchange_latency`는 **캐시 HIT/MISS를 구분하지 않고** `main.py`의 정상 경로(`try` 블록) 전체 소요시간을 측정한다 — 순수 캐시 조회만의 지연시간과 실제 Keycloak 왕복시간이 같은 히스토그램에 섞여 기록된다는 점은 참고할 부분이다(캐시 HIT 시에는 `cache.get_or_exchange_jwt`가 거의 즉시 반환되므로 값 자체는 매우 작게 찍힌다).
- 서킷 브레이커 OPEN 상태에서 캐시로 응답하는 경로(`main.py`의 `is_circuit_open` 분기)는 `token_exchange_latency`를 기록하지 않고 `token_exchange_total{result="hit"}`만 증가시킨다 — 정상 경로와 서킷-오픈 경로의 메트릭 기록 방식이 다르다.

---

## 9. `Dockerfile` / `requirements.txt`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY services/token-exchange-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY infra/redis/ /app/infra/redis/          # redis_client.load_lua_scripts()가 찾는 경로(/app/infra/redis/unlock_if_mine.lua)를 만들어줌
COPY services/token-exchange-service/ .
EXPOSE 8081
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081"]
```

```
fastapi==0.115.0            # 웹 프레임워크
uvicorn[standard]==0.30.6   # ASGI 서버
redis[hiredis]==5.0.8       # Redis 클라이언트(hiredis 파서로 파싱 가속)
httpx==0.27.2                # Keycloak 호출용 비동기 HTTP 클라이언트
python-jose[cryptography]==3.3.0  # JWT 클레임 파싱(_extract_jti)
tenacity==8.5.0               # 재시도 라이브러리 (선언되어 있으나 현재 코드에서 실제 사용처는 없음)
prometheus-client==0.21.0    # /metrics 노출
python-dotenv==1.0.1          # .env 파일 로드 지원
pydantic-settings==2.5.2      # Settings 클래스 기반
```

**핵심 포인트**
- `infra/redis/`를 이미지 안 `/app/infra/redis/`에 통째로 복사하기 때문에, `redis_client.py`가 상대경로(`Path(__file__).parent / "infra/redis/unlock_if_mine.lua"`)로 스크립트를 찾을 수 있다.
- `tenacity`가 의존성에는 있지만 실제 재시도 데코레이터(`@retry`)가 코드 어디에도 적용되어 있지 않다 — `config.py`의 `retry_max_attempts`/`retry_wait_ms`와 함께, 재시도 기능이 아직 구현되지 않았거나 제거된 흔적으로 보인다.

---

## 전체적인 설계 관찰

1. **역할이 명확히 3겹으로 나뉜다**: 호출자 인증(`caller_auth`) → 캐시/동시성 제어(`cache`) → 실제 프로토콜 통신(`keycloak_client`). 각 모듈은 서로의 내부 구현을 몰라도 되게 얇은 인터페이스로 연결되어 있다.
2. **모든 상태를 Redis에 위임**: JWT 캐시, single-flight 락, 서킷 브레이커 상태까지 전부 프로세스 메모리가 아닌 Redis에 저장해, TXS를 여러 인스턴스로 무상태(stateless) 수평 확장할 수 있게 설계되어 있다.
3. **캐시 스탬피드 대비가 이중으로 되어 있다**: ① 캐시 미스 시 single-flight 락, ② 캐시 만료 임박 시 확률적 조기 갱신 — 두 메커니즘이 서로 다른 시점(미스 직후 vs. 만료 직전)의 몰림 현상을 각각 막는다.
4. **장애 시에도 서비스 지속을 우선**: 서킷 브레이커가 열려도 캐시된 JWT가 있으면 계속 응답하는 것을 선택해, Keycloak 순단 상황에서도 이미 발급된 토큰을 쓰는 사용자는 영향을 받지 않도록 했다.
5. **미완성/미사용으로 보이는 부분들**: `redis_client.get_unlock_sha()`(미사용), `_quota_sha`(미사용), `retry_max_attempts`/`retry_wait_ms` + `tenacity` 의존성(재시도 로직 미구현), `main.py`의 서킷-오픈 분기에서 `cache._scope_hash`를 재사용하지 않고 동일 로직을 인라인으로 재구현한 점 — 기능 동작에는 영향이 없지만, 향후 리팩터링 시 정리 대상으로 참고할 만하다.
