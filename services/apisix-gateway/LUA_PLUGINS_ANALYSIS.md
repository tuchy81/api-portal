# APISIX Lua 플러그인 분석 문서

> 이 문서는 `services/apisix-gateway` 하위의 Lua 스크립트(APISIX 커스텀 플러그인 + Redis 스크립트)를 읽고 분석한 결과입니다.
> **원본 `.lua` 파일은 수정하지 않았으며**, 아래 코드 블록은 분석용으로 주석을 덧붙인 사본입니다.

## 대상 파일 목록

| 파일 | 역할 | 실행 phase / priority |
|---|---|---|
| `plugins/citizen/common.lua` | 4개 플러그인이 공유하는 헬퍼 모듈 (라이브러리, 플러그인 아님) | - |
| `plugins/apisix/plugins/pat-auth.lua` | PAT(Personal Access Token) 인증/인가 | `rewrite`, priority `3010` |
| `plugins/apisix/plugins/pat-quota.lua` | Rate limit + 일/월 쿼터 검사 | `access`, priority `3005` |
| `plugins/apisix/plugins/pat-token-exchange.lua` | PAT → 내부 JWT 교환, 헤더 주입 | `access`, priority `3000` |
| `plugins/apisix/plugins/pat-audit.lua` | 감사 로그 비동기 배치 전송 | `log`, priority `100` |
| `infra/redis/quota_deduct.lua` | Redis 서버 측 원자적 쿼터 차감 스크립트 | Redis `EVALSHA` |
| `infra/redis/unlock_if_mine.lua` | Redis 분산 락 안전 해제 스크립트 (compare-and-delete) | Redis `EVALSHA` (token-exchange-service 파이썬 코드에서 사용) |

전체 흐름(요청 1건 기준):

```
클라이언트 (Authorization: Bearer hdpat_xxx...)
      │
      ▼
[rewrite] pat-auth (3010)          PAT 파싱 → Redis/포털 조회 → 상태/만료/HMAC/CIDR/scope 검증
      │  (ctx.cdp_* 값들을 세팅)
      ▼
[access]  pat-quota (3005)         cdp:rl / cdp:quota:d / cdp:quota:m 원자적 검사·차감 (quota_deduct.lua)
      │
      ▼
[access]  pat-token-exchange(3000) PAT → 내부 JWT 캐시 조회/교환, 스푸핑 가능 헤더 제거, 헤더 주입
      │
      ▼
      업스트림(내부 API)으로 프록시
      │
      ▼
[log]     pat-audit (100)          위 단계 중 성공/실패 여부와 무관하게 이벤트를 배치로 모아 portal-backend에 비동기 전송
```

APISIX는 같은 요청 내에서 `rewrite` phase를 모든 `access` phase보다 먼저 실행하므로, `priority` 값은 **같은 phase 안에서만** 순서를 결정합니다(숫자가 클수록 먼저 실행). 따라서 실제 실행 순서는 `pat-auth → pat-quota → pat-token-exchange → pat-audit` 입니다.

---

## 1. `plugins/citizen/common.lua` — 공유 헬퍼 모듈

역할: 나머지 4개 플러그인이 반복 구현하면 위험한 보안 민감 로직(HMAC 비교, 문제 응답 포맷, PAT 마스킹)을 한 곳에 모아둔 라이브러리.

```lua
-- Shared helpers for the citizen-gateway custom plugins (pat-auth, pat-quota,
-- pat-token-exchange, pat-audit). Kept in one place so the four plugins stay
-- thin and the security-sensitive bits (HMAC compare, problem+json shape,
-- PAT masking) have a single implementation.
local core   = require("apisix.core")
local redis  = require("resty.redis")
local sha256 = require("resty.sha256")
local resty_str = require("resty.string")
local bit    = require("bit")           -- LuaJIT 전용 비트연산 라이브러리
local cjson  = require("cjson.safe")    -- 예외를 던지지 않는 안전한 JSON 인코더/디코더

local ngx = ngx
local str_char = string.char
local str_byte = string.byte
local str_rep  = string.rep

local _M = {}  -- 이 모듈이 외부로 노출하는 함수/상수를 담는 테이블(관례적 이름)

-- PAT(Personal Access Token) 형식 검증용 정규식.
-- 예: "Bearer hdpat_AbCdEfGhIjKl_43자리시크릿..."
-- 캡처그룹 1 = 12자 tokenId, 캡처그룹 2 = 43자 secret
_M.PAT_REGEX = [[^Bearer\s+hdpat_([A-Za-z0-9]{12})_([A-Za-z0-9_-]{43})$]]

-- ---------------------------------------------------------------------------
-- HMAC-SHA256 (hex), implemented on top of resty.sha256 so we don't depend
-- on resty.hmac being present in every APISIX build. Verified against
-- Python's hmac.new(key, msg, hashlib.sha256).hexdigest() during development.
-- ---------------------------------------------------------------------------
-- RFC 2104 표준 HMAC 알고리즘을 sha256 원시 함수로 직접 구현.
-- (일부 APISIX 배포판에 resty.hmac 모듈이 없을 수 있어 의존성을 줄이려는 목적)
function _M.hmac_sha256_hex(key, msg)
    local blocksize = 64  -- SHA-256의 블록 크기(바이트)
    -- 1) 키가 블록 크기보다 길면 먼저 해시해서 축소
    if #key > blocksize then
        local h = sha256:new()
        h:update(key)
        key = h:final()
    end
    -- 2) 키가 블록 크기보다 짧으면 0x00으로 패딩
    if #key < blocksize then
        key = key .. str_rep(str_char(0), blocksize - #key)
    end

    -- 3) ipad(0x36)/opad(0x5c)와 키를 XOR
    local ipad, opad = {}, {}
    for i = 1, blocksize do
        local kb = str_byte(key, i)
        ipad[i] = str_char(bit.bxor(kb, 0x36))
        opad[i] = str_char(bit.bxor(kb, 0x5c))
    end
    ipad = table.concat(ipad)
    opad = table.concat(opad)

    -- 4) inner = SHA256(ipad || message)
    local inner = sha256:new()
    inner:update(ipad)
    inner:update(msg)
    local inner_digest = inner:final()

    -- 5) outer = SHA256(opad || inner) → 최종 HMAC, 16진 문자열로 반환
    local outer = sha256:new()
    outer:update(opad)
    outer:update(inner_digest)
    return resty_str.to_hex(outer:final())
end

-- Constant-time compare (avoid leaking timing info on PAT secret checks).
-- 타이밍 사이드채널 공격 방지용 상수 시간 비교.
-- 문자열 길이가 다르면 즉시 false를 반환하지만(길이 자체는 비밀이 아니라고 간주),
-- 같은 길이일 때는 모든 바이트를 끝까지 비교해 "몇 번째 바이트에서 틀렸는지"가
-- 비교 소요 시간으로 드러나지 않도록 XOR을 누적(OR)한다.
function _M.constant_time_eq(a, b)
    if type(a) ~= "string" or type(b) ~= "string" or #a ~= #b then
        return false
    end
    local diff = 0
    for i = 1, #a do
        diff = bit.bor(diff, bit.bxor(str_byte(a, i), str_byte(b, i)))
    end
    return diff == 0
end

-- 단순 SHA-256 hex 해시 (HMAC이 아닌 일반 해시가 필요한 곳: trace id 생성, scope 해시 등)
function _M.sha256_hex(msg)
    local h = sha256:new()
    h:update(msg)
    return resty_str.to_hex(h:final())
end

-- ---------------------------------------------------------------------------
-- Trace id — 32 lowercase hex chars, mirrors uuid4().hex[:32] from the
-- previous FastAPI gateway. Not security sensitive, just needs to be unique
-- enough for correlating audit/log lines.
-- ---------------------------------------------------------------------------
local trace_seq = 0  -- 워커 프로세스별 단조 증가 카운터(모듈 로드 시 1회 초기화, 워커 생존 기간 유지)
function _M.gen_trace_id()
    trace_seq = trace_seq + 1
    -- 현재시각 + PID + 시퀀스 + 난수를 섞어 해시 → 32자 hex로 자름
    -- (구 FastAPI 게이트웨이가 쓰던 uuid4().hex[:32] 형식과 길이를 맞추기 위함이며,
    --  암호학적 무작위성이 필요한 값이 아니라 로그 상관관계 추적용 식별자일 뿐)
    local raw = ngx.now() .. "|" .. tostring(ngx.worker.pid()) .. "|" ..
                tostring(trace_seq) .. "|" .. tostring(math.random(1, 1e9))
    return _M.sha256_hex(raw):sub(1, 32)
end

-- ---------------------------------------------------------------------------
-- RFC 9457 problem+json helper. Returns (status, body_table) — hand the
-- result straight back from a plugin phase function, e.g.:
--   return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "...")
-- ---------------------------------------------------------------------------
-- RFC 9457(Problem Details for HTTP APIs) 표준 형식의 에러 응답 바디를 생성.
-- 플러그인의 rewrite/access 함수가 `return status, body`를 하면
-- APISIX가 그 즉시 요청을 중단하고 해당 응답을 클라이언트에 반환한다.
function _M.problem_json(ctx, status, code, title, detail, extra_headers)
    if ctx then
        ctx.cdp_error_code = code  -- pat-audit의 log phase에서 이벤트 분류(classify_event)에 사용
    end
    core.response.set_header("Content-Type", "application/problem+json")
    if extra_headers then
        for k, v in pairs(extra_headers) do
            core.response.set_header(k, tostring(v))
        end
    end
    local instance = (ctx and ctx.var and ctx.var.request_uri) or "/"
    return status, {
        type = "https://cdp-portal.hd.com/errors/" .. code,
        title = title,
        status = status,
        code = code,
        detail = detail,
        instance = instance,
    }
end

-- ---------------------------------------------------------------------------
-- Redis
-- ---------------------------------------------------------------------------
-- Redis 커넥션을 생성/연결하고 필요 시 AUTH까지 수행하는 공통 함수.
-- 실패 시 (nil, err)를 반환하여 호출부에서 503 처리하도록 한다.
function _M.redis_connect(host, port, password, timeout_ms)
    local red = redis:new()
    red:set_timeout(timeout_ms or 2000)
    local ok, err = red:connect(host, port)
    if not ok then
        return nil, err
    end
    if password and password ~= "" then
        local ok2, err2 = red:auth(password)
        if not ok2 then
            return nil, err2
        end
    end
    return red, nil
end

-- 매 요청마다 새 TCP 연결을 맺지 않도록 커넥션 풀에 반환(keepalive).
-- 10초 유휴 타임아웃, 최대 100개 커넥션을 풀에 유지.
function _M.redis_keepalive(red)
    if red then
        local ok, err = red:set_keepalive(10000, 100)
        if not ok then
            core.log.warn("failed to set redis keepalive: ", err)
        end
    end
end

-- Convert resty.redis HGETALL's flat array {f1,v1,f2,v2,...} into a table.
-- Returns nil if the hash didn't exist (empty array).
-- resty.redis의 HGETALL은 {field1, value1, field2, value2, ...} 형태의
-- 평탄화된 배열을 반환하므로, 이를 {field1=value1, field2=value2, ...}
-- 형태의 lookup table로 변환한다. 해시가 존재하지 않으면(빈 배열) nil 반환.
function _M.hgetall_to_table(res)
    if type(res) ~= "table" or #res == 0 then
        return nil
    end
    local t = {}
    for i = 1, #res, 2 do
        t[res[i]] = res[i + 1]
    end
    return t
end

-- ---------------------------------------------------------------------------
-- PAT plaintext masking — never let hdpat_<id>_<secret> reach a log/audit
-- sink in full. Keeps the tokenId prefix (harmless, already public-ish) and
-- blanks the secret half.
-- ---------------------------------------------------------------------------
-- 로그/감사 데이터에 PAT 원문이 그대로 남지 않도록 정규식으로 secret 부분만 마스킹.
-- "hdpat_<12자tokenId>_<43자secret>" → "hdpat_<12자tokenId>_****"
function _M.mask_pat(text)
    if not text or text == "" then
        return text
    end
    local masked = ngx.re.gsub(text, [[(hdpat_[A-Za-z0-9]{12}_)[A-Za-z0-9_-]{43}]], "$1****", "jo")
    return masked
end

-- Unverified JWT payload peek, just to pull `jti` for audit correlation
-- (the JWT was already verified by the internal gateway upstream; the
-- gateway itself only needs the claim, not to re-validate the signature).
-- JWT의 payload(2번째 세그먼트)만 base64url 디코딩해 `jti` 클레임을 꺼낸다.
-- 서명 검증은 하지 않음 — 이 JWT는 이미 내부 게이트웨이가 검증한 것이므로
-- 여기서는 감사 로그 상관관계 용도로 클레임 값만 필요하기 때문.
function _M.extract_jwt_jti(jwt)
    if not jwt then
        return nil
    end
    -- "header.payload.signature" 중 payload만 정규식으로 추출
    local payload_b64 = jwt:match("^[^.]+%.([^.]+)%.")
    if not payload_b64 then
        return nil
    end
    -- base64url → base64 표준 문자셋으로 치환 후 '=' 패딩 복원
    payload_b64 = payload_b64:gsub("-", "+"):gsub("_", "/")
    local pad = #payload_b64 % 4
    if pad == 2 then
        payload_b64 = payload_b64 .. "=="
    elseif pad == 3 then
        payload_b64 = payload_b64 .. "="
    end
    local raw = ngx.decode_base64(payload_b64)
    if not raw then
        return nil
    end
    local data = cjson.decode(raw)
    if not data then
        return nil
    end
    return data.jti
end

return _M
```

**핵심 포인트**
- HMAC은 라이브러리 의존성을 줄이려고 `resty.sha256` 원시 함수로 RFC 2104를 직접 구현했다.
- `constant_time_eq`는 PAT 시크릿 비교 시 타이밍 공격을 막기 위한 상수시간 비교다.
- `problem_json`은 RFC 9457(Problem Details) 포맷을 강제해 4개 플러그인의 에러 응답 형태를 통일한다.
- `mask_pat` / `extract_jwt_jti`는 순전히 감사 로그(`pat-audit.lua`)를 위한 보조 함수다.

---

## 2. `plugins/apisix/plugins/pat-auth.lua` — PAT 인증/인가 플러그인

**실행 시점**: `rewrite` phase, priority `3010` (커스텀 플러그인 중 가장 먼저 실행).
**역할**: `Authorization: Bearer hdpat_<id>_<secret>` 헤더를 파싱하고, Redis(캐시) → 실패 시 portal-backend(원본 저장소) 순으로 PAT 메타데이터를 조회한 뒤 상태/만료/서명(HMAC)/CIDR/스코프를 검증한다. 통과하면 이후 플러그인(`pat-quota`, `pat-token-exchange`)이 쓸 컨텍스트 값을 `ctx.cdp_*`에 저장한다.

```lua
-- pat-auth — Citizen Developer Portal gateway plugin.
-- Implements spec section 6.2: PAT parsing, Redis-cached PAT metadata lookup
-- (with portal-backend fallback + negative cache), status/expiry/HMAC/CIDR
-- checks and required-scope enforcement. Runs in the `rewrite` phase, ahead
-- of pat-quota / pat-token-exchange.
local core   = require("apisix.core")
local http   = require("resty.http")
local cjson  = require("cjson.safe")
local common = require("citizen.common")
local bit    = require("bit")

local ngx = ngx

-- APISIX가 라우트/서비스 설정(admin API)에서 이 플러그인 설정을 검증할 때 쓰는 JSON 스키마.
local schema = {
    type = "object",
    properties = {
        redis_host = { type = "string", default = "redis" },
        redis_port = { type = "integer", default = 6379 },
        redis_password = { type = "string", default = "" },
        server_key = { type = "string" },          -- HMAC 검증용 서버 비밀키
        portal_backend_url = { type = "string" },  -- Redis 미스 시 폴백 조회 대상
        internal_api_key = { type = "string", default = "" },
        neg_cache_ttl = { type = "integer", default = 30 }, -- 존재하지 않는 토큰에 대한 네거티브 캐시 TTL(초)
        -- HTTP method -> required scope name, for THIS route only
        -- (the route's own `uri`/`uris` already pins the path).
        required_scope_map = {
            type = "object",
            minProperties = 1,
        },
    },
    required = { "server_key", "portal_backend_url", "required_scope_map" },
}

local _M = {
    version = 0.1,
    priority = 3010,   -- rewrite phase 내에서 실행 순서(숫자가 클수록 먼저 실행)
    name = "pat-auth",
    schema = schema,
}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

-- 클라이언트의 실제 IP를 결정. X-Forwarded-For가 있으면 그 첫 값(가장 왼쪽 = 원 클라이언트),
-- 없으면 nginx의 remote_addr을 사용.
-- 주의: X-Forwarded-For는 클라이언트가 조작 가능한 헤더이므로, 앞단에 신뢰할 수 있는
-- 리버스 프록시가 있다는 전제 하에서만 안전하다(이 저장소 범위 밖의 배포 이슈).
local function get_client_ip(ctx)
    local xff = core.request.header(ctx, "X-Forwarded-For")
    if xff then
        local first = xff:match("^%s*([^,]+)")
        if first and first ~= "" then
            return first
        end
    end
    return ctx.var.remote_addr or "0.0.0.0"
end

-- Only IPv4 CIDRs are supported (matches the previous implementation's
-- scope). A non-IPv4 client against a configured allow-list is rejected.
-- Note: LuaJIT has no `&`/`~`/`|` operators — bitwise ops go through the
-- `bit` library, which works on 32-bit signed ints (2's complement), fine
-- here since both sides go through the same mask before comparison.
-- IPv4 4옥텟(a.b.c.d)을 32비트 정수 하나로 합친다. (예: 192.168.0.1 → 정수 하나)
local function to_int(a, b, c, d)
    return bit.bor(
        bit.lshift(tonumber(a), 24),
        bit.lshift(tonumber(b), 16),
        bit.lshift(tonumber(c), 8),
        tonumber(d)
    )
end

-- CIDR 프리픽스 길이(bits)로부터 서브넷 마스크(32비트 정수)를 계산.
-- 0 이하 → 마스크 전체 0(모든 IP 매치), 32 이상 → 마스크 전체 1(정확히 일치해야 함).
local function cidr_mask(bits)
    if bits <= 0 then
        return 0
    end
    if bits >= 32 then
        return bit.bnot(0)
    end
    return bit.lshift(bit.bnot(0), 32 - bits)
end

-- 주어진 ip가 cidr(예: "10.0.0.0/8") 범위 안에 있는지 확인.
-- cidr 문자열이 CIDR 표기(prefix/bits)가 아니면 단순 문자열 완전일치로 비교.
local function ip_in_cidr(ip, cidr)
    local ip_a, ip_b, ip_c, ip_d = ip:match("^(%d+)%.(%d+)%.(%d+)%.(%d+)$")
    if not ip_a then
        return false
    end
    local net, bits = cidr:match("^(%d+%.%d+%.%d+%.%d+)/(%d+)$")
    if not net then
        return ip == cidr
    end
    bits = tonumber(bits)
    local n_a, n_b, n_c, n_d = net:match("^(%d+)%.(%d+)%.(%d+)%.(%d+)$")
    local ip_int = to_int(ip_a, ip_b, ip_c, ip_d)
    local net_int = to_int(n_a, n_b, n_c, n_d)
    local mask = cidr_mask(bits)
    -- 두 IP를 같은 마스크로 AND한 뒤(네트워크 부분만 남김) 비교
    return bit.band(ip_int, mask) == bit.band(net_int, mask)
end

-- 허용 CIDR 목록이 비어있으면(= 제한 없음) 무조건 통과. 있으면 하나라도 매치되면 통과.
local function check_cidr(ip, allowed_cidr)
    if not allowed_cidr or #allowed_cidr == 0 then
        return true
    end
    for _, cidr in ipairs(allowed_cidr) do
        if ip_in_cidr(ip, cidr) then
            return true
        end
    end
    return false
end

-- Current UTC time as an ISO-8601 string in the same "+00:00" shape the
-- portal backend writes (`datetime.isoformat()` on a tz-aware UTC value).
-- Fixed-width fields mean plain string comparison against `expires_at`
-- sorts correctly regardless of fractional-second noise on either side.
-- portal-backend(Python)이 저장하는 `expires_at` 문자열 포맷과 동일하게 맞춰서
-- 별도 날짜 파싱 없이 "문자열 비교"만으로 만료 여부를 판단할 수 있게 한다.
local function now_iso_utc()
    return ngx.utctime():gsub(" ", "T") .. "+00:00"
end

-- Fallback: ask portal-backend for PAT metadata on a Redis cache miss.
-- Mirrors plugins/pat_auth.py's behaviour, including the fact that HMAC
-- verification is skipped for this path (no HMAC ships in the fallback
-- payload — only Redis, populated at issuance time, has it).
-- Redis 캐시에 PAT 정보가 없을 때(=최초 조회거나 캐시 만료) portal-backend의
-- 내부 API(`/internal/pat/{tokenId}`)를 호출해 원본 메타데이터를 가져온다.
-- 주의: 이 경로로 받은 데이터에는 hash(HMAC)가 없으므로(hash="") 아래 rewrite()에서
-- HMAC 검증 단계가 자연히 스킵된다 — Redis가 캐시 미스일 때 서명 검증을 생략하는
-- 것은 구현 상의 의도된 트레이드오프(원본 발급 시점에만 hash가 기록됨).
local function fetch_from_portal(conf, token_id)
    local headers = { ["Accept"] = "application/json" }
    if conf.internal_api_key and conf.internal_api_key ~= "" then
        headers["X-Internal-Key"] = conf.internal_api_key
    end

    local httpc = http.new()
    httpc:set_timeout(3000)
    local res, err = httpc:request_uri(conf.portal_backend_url .. "/internal/pat/" .. token_id, {
        method = "GET",
        headers = headers,
    })
    if not res then
        return nil, err
    end
    if res.status ~= 200 then
        return nil, "status " .. res.status
    end
    local data, jerr = cjson.decode(res.body)
    if not data then
        return nil, jerr
    end
    -- Redis HGETALL 결과와 동일한 필드 구조(테이블)로 정규화해서 돌려준다.
    -- 이렇게 하면 rewrite()의 이후 로직이 "Redis에서 왔는지 portal에서 왔는지"를
    -- 신경 쓰지 않고 동일하게 처리할 수 있다.
    return {
        sub = data.sub,
        scopes = cjson.encode(data.scopes or {}),
        status = data.status,
        cidr = cjson.encode(data.cidr or {}),
        hash = "",  -- portal 폴백 경로에는 HMAC 해시가 없음
        rate_limit_tps = tostring((data.quota and data.quota.rateLimitTps) or 10),
        burst = tostring((data.quota and data.quota.burst) or 20),
        daily_quota = tostring((data.quota and data.quota.dailyQuota) or 5000),
        monthly_quota = tostring((data.quota and data.quota.monthlyQuota) or 100000),
        expires_at = data.expiresAt or "",
    }, nil
end

-- APISIX가 rewrite phase에서 호출하는 진입점.
-- 반환값이 있으면(= status, body) APISIX가 즉시 그 응답으로 요청을 종료한다.
-- 반환값이 없으면(nil) 다음 단계(access phase의 pat-quota 등)로 요청이 이어진다.
function _M.rewrite(conf, ctx)
    -- 1) Authorization 헤더에서 PAT 형식(hdpat_<id>_<secret>) 파싱
    local authorization = core.request.header(ctx, "Authorization") or ""
    local m = ngx.re.match(authorization, common.PAT_REGEX, "jo")
    if not m then
        return common.problem_json(ctx, 401, "CDP-1002",
            "Missing or invalid Authorization header",
            "Expected: Bearer hdpat_<12>_<43>")
    end
    local token_id, secret = m[1], m[2]

    -- Populate the audit context as soon as the token is parsed, not after the
    -- checks pass. Rejections are exactly the events worth attributing: without
    -- this, SCOPE_DENIED/AUTH_FAILED rows carry a NULL token_id, which drops
    -- them from the daily error-rate rollup and makes takeover attempts
    -- untraceable. The tokenId half of a PAT is an identifier, not the secret.
    -- 검증에 실패해도 "누가 실패했는지"를 감사 로그에 남기기 위해, 검증 통과 여부와
    -- 무관하게 파싱 직후 토큰ID/클라이언트IP/트레이스ID를 먼저 ctx에 채워둔다.
    -- (tokenId는 시크릿이 아니라 식별자이므로 노출해도 안전)
    ctx.cdp_token_id = token_id
    ctx.cdp_client_ip = get_client_ip(ctx)
    ctx.cdp_trace_id = common.gen_trace_id()

    -- 2) Redis 연결
    local red, rerr = common.redis_connect(conf.redis_host, conf.redis_port, conf.redis_password)
    if not red then
        core.log.error("pat-auth: redis connect failed: ", rerr)
        return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "redis unavailable")
    end

    -- 3) Redis에서 "cdp:pat:{tokenId}" 해시로 PAT 메타데이터 조회 (캐시 조회)
    local res, err = red:hgetall("cdp:pat:" .. token_id)
    if err then
        common.redis_keepalive(red)
        core.log.error("pat-auth: redis hgetall failed: ", err)
        return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "redis error")
    end
    local meta = common.hgetall_to_table(res)

    if not meta then
        -- 4) 캐시 미스 → 먼저 "네거티브 캐시"(존재하지 않는다고 이미 확인된 토큰)를 확인해
        --    매번 portal-backend를 때리지 않도록 방어
        local neg_key = "cdp:pat:neg:" .. token_id
        local neg, _ = red:get(neg_key)
        if neg and neg ~= ngx.null then
            common.redis_keepalive(red)
            return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "token not found (negative cache)")
        end

        -- 5) 네거티브 캐시에도 없으면 portal-backend에 원본 조회 (폴백)
        local fetched, ferr = fetch_from_portal(conf, token_id)
        if not fetched then
            -- 존재하지 않는 토큰으로 확인되면 neg_cache_ttl초 동안 네거티브 캐싱
            red:setex(neg_key, conf.neg_cache_ttl, "1")
            common.redis_keepalive(red)
            core.log.info("pat-auth: portal fallback miss for ", token_id, ": ", ferr)
            return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "token not found")
        end
        meta = fetched
    end

    -- Known from here on, so rejections below are attributable to a user too.
    ctx.cdp_user_sub = meta.sub

    -- 6) 상태 검사: ACTIVE가 아니면(REVOKED, DISABLED 등) 거부
    if meta.status ~= "ACTIVE" then
        common.redis_keepalive(red)
        return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "Token status is " .. tostring(meta.status))
    end

    -- 7) 만료 검사: expires_at이 있고 현재시각보다 과거면 거부 (문자열 비교로 충분한 이유는 now_iso_utc 주석 참고)
    if meta.expires_at and meta.expires_at ~= "" and meta.expires_at < now_iso_utc() then
        common.redis_keepalive(red)
        return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "Token has expired")
    end

    -- 8) HMAC 서명 검증: meta.hash가 있는 경우에만(Redis 캐시 경로) 시크릿의 HMAC을 재계산해 비교.
    --    portal 폴백 경로는 hash=""이므로 이 블록 자체가 스킵된다(위 fetch_from_portal 주석 참고).
    if meta.hash and meta.hash ~= "" then
        local expected = common.hmac_sha256_hex(conf.server_key, secret)
        if not common.constant_time_eq(expected, meta.hash) then
            common.redis_keepalive(red)
            return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "Token signature mismatch")
        end
    end

    -- 9) 클라이언트 IP가 허용 CIDR 목록에 있는지 검사
    local client_ip = ctx.cdp_client_ip
    local allowed_cidr = cjson.decode(meta.cidr or "[]") or {}
    if not check_cidr(client_ip, allowed_cidr) then
        common.redis_keepalive(red)
        return common.problem_json(ctx, 403, "CDP-1006",
            "Client IP not allowed",
            "Client IP " .. client_ip .. " not in allowed CIDR")
    end

    -- 10) 이 라우트+HTTP메서드에 필요한 scope가 PAT에 부여되어 있는지 검사
    --     (required_scope_map에 해당 메서드가 없으면 scope 체크 자체를 생략 = 통과)
    local required_scope = conf.required_scope_map[ctx.var.request_method]
    local granted_scopes = cjson.decode(meta.scopes or "[]") or {}
    local has_scope = false
    if required_scope then
        for _, s in ipairs(granted_scopes) do
            if s == required_scope then
                has_scope = true
                break
            end
        end
    else
        has_scope = true
    end
    if not has_scope then
        common.redis_keepalive(red)
        return common.problem_json(ctx, 403, "CDP-1003",
            "Required scope not granted",
            "Required scope '" .. tostring(required_scope) .. "' not granted")
    end

    common.redis_keepalive(red)

    -- token_id / user_sub / client_ip / trace_id are already on ctx; only the
    -- quota inputs for pat-quota remain.
    -- 모든 검증 통과 → 이후 단계(pat-quota, pat-token-exchange)가 쓸 값들을 ctx에 저장
    ctx.cdp_scopes = granted_scopes
    ctx.cdp_rate_limit_tps = tonumber(meta.rate_limit_tps) or 10
    ctx.cdp_burst = tonumber(meta.burst) or 20
    ctx.cdp_daily_quota = tonumber(meta.daily_quota) or 5000
    ctx.cdp_monthly_quota = tonumber(meta.monthly_quota) or 100000
end

return _M
```

**검증 순서 요약**: 헤더 형식 → (Redis 캐시 → 없으면 portal-backend 폴백 + 네거티브 캐시) → 상태(ACTIVE) → 만료 → HMAC 서명 → CIDR → 필요 scope. 하나라도 실패하면 즉시 RFC 9457 형식의 에러를 반환하고 요청을 중단한다.

---

## 3. `plugins/apisix/plugins/pat-quota.lua` — Rate limit / 쿼터 플러그인

**실행 시점**: `access` phase, priority `3005` (pat-auth 다음, pat-token-exchange 이전).
**역할**: 토큰버킷 방식의 초당 rate limit과 일간/월간 호출 쿼터를 Redis 스크립트(`quota_deduct.lua`)로 원자적으로 검사·차감한다.

```lua
-- pat-quota — Citizen Developer Portal gateway plugin.
-- Implements spec section 6.3: atomic token-bucket + daily/monthly quota
-- deduction via the shared `infra/redis/quota_deduct.lua` script (single
-- source of truth, same file the old FastAPI gateway and this plugin both
-- EVALSHA). Runs in the `access` phase, right after pat-auth's `rewrite`.
local core   = require("apisix.core")
local common = require("citizen.common")

-- APISIX 컨테이너 내부에 마운트되어 있는 Redis 스크립트 원본 경로.
-- infra/redis/quota_deduct.lua 와 동일한 내용이 배포 시 이 경로에 복사/마운트된다.
local QUOTA_SCRIPT_PATH = "/opt/redis-scripts/quota_deduct.lua"

local quota_script_text  -- 스크립트 원문 캐시(워커 프로세스 생애주기 동안 1회만 파일 읽기)
local quota_sha -- cached per worker process; reloaded on NOSCRIPT

-- 스크립트 파일을 최초 1회만 디스크에서 읽고, 이후로는 메모리 캐시(quota_script_text)를 재사용.
local function load_script_text()
    if quota_script_text then
        return quota_script_text
    end
    local f, ferr = io.open(QUOTA_SCRIPT_PATH, "r")
    if not f then
        core.log.error("pat-quota: cannot open quota script at ", QUOTA_SCRIPT_PATH, ": ", ferr)
        return nil
    end
    quota_script_text = f:read("*a")
    f:close()
    return quota_script_text
end

local schema = {
    type = "object",
    properties = {
        redis_host = { type = "string", default = "redis" },
        redis_port = { type = "integer", default = 6379 },
        redis_password = { type = "string", default = "" },
    },
}

local _M = {
    version = 0.1,
    priority = 3005,
    name = "pat-quota",
    schema = schema,
}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

-- UTC 기준 자정까지 남은 초 (일간 쿼터 키의 TTL/Retry-After 계산용)
local function seconds_until_midnight_utc()
    local t = os.date("!*t")
    local elapsed = t.hour * 3600 + t.min * 60 + t.sec
    return 86400 - elapsed
end

-- 윤년 판정 (그레고리력 규칙: 4의 배수이면서 100의 배수가 아니거나, 400의 배수)
local function is_leap(year)
    return (year % 4 == 0) and (year % 100 ~= 0 or year % 400 == 0)
end

local DAYS_IN_MONTH = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 }

-- UTC 기준 이번 달 말일 자정까지 남은 초 (월간 쿼터 키의 TTL/Retry-After 계산용)
local function seconds_until_month_end_utc()
    local t = os.date("!*t")
    local days_in_month = DAYS_IN_MONTH[t.month]
    if t.month == 2 and is_leap(t.year) then
        days_in_month = 29
    end
    local elapsed_today = t.hour * 3600 + t.min * 60 + t.sec
    local days_left = days_in_month - t.day
    return math.max(0, days_left * 86400 + (86400 - elapsed_today))
end

-- APISIX access phase 진입점. 반환값이 있으면 즉시 응답 후 요청 중단.
function _M.access(conf, ctx)
    local token_id = ctx.cdp_token_id
    if not token_id then
        -- pat-auth should always run first and exit on failure; this would
        -- only happen if the route is misconfigured without pat-auth.
        -- 정상 배포라면 pat-auth가 항상 먼저 실행되어 ctx.cdp_token_id를 채우므로
        -- 이 분기는 "라우트에 pat-auth가 빠진 설정 오류"를 잡아내는 방어 코드다.
        return common.problem_json(ctx, 500, "CDP-9000", "Gateway misconfigured", "pat-quota ran without pat-auth context")
    end

    local today = os.date("!%Y%m%d")
    local month = os.date("!%Y%m")

    -- Redis 키 3종: 초당 토큰버킷 상태, 일간 카운터, 월간 카운터
    local rl_key = "cdp:rl:" .. token_id
    local daily_key = "cdp:quota:d:" .. token_id .. ":" .. today
    local monthly_key = "cdp:quota:m:" .. token_id .. ":" .. month

    -- pat-auth가 ctx에 채워둔 이 PAT의 요율/쿼터 설정값들
    local tps = ctx.cdp_rate_limit_tps
    local burst = ctx.cdp_burst
    local daily_quota = ctx.cdp_daily_quota
    local monthly_quota = ctx.cdp_monthly_quota
    local now_ms = math.floor(ngx.now() * 1000)
    -- TTL은 자정/월말까지 남은 시간 + 300초 여유. 클럭 스큐나 지연 요청으로 인해
    -- 카운터가 경계 시점 직전에 만료되어 카운트가 씹히는 것을 방지하기 위한 버퍼.
    local daily_ttl = seconds_until_midnight_utc() + 300
    local monthly_ttl = seconds_until_month_end_utc() + 300

    local red, cerr = common.redis_connect(conf.redis_host, conf.redis_port, conf.redis_password)
    if not red then
        core.log.error("pat-quota: redis connect failed: ", cerr)
        return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "redis unavailable")
    end

    local script = load_script_text()
    if not script then
        common.redis_keepalive(red)
        return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "quota script unavailable")
    end

    -- Redis Lua 스크립트를 EVALSHA로 실행(캐시된 SHA가 있으면 스크립트 본문 전송 없이 실행,
    -- 네트워크 대역폭 절약). 스크립트가 Redis 서버 메모리에서 아직 로드되지 않았거나(NOSCRIPT)
    -- 이 워커가 처음 실행하는 경우엔 quota_sha가 nil이므로 아래 블록에서 SCRIPT LOAD를 수행한다.
    local result, rerr
    if quota_sha then
        result, rerr = red:evalsha(quota_sha, 3, rl_key, daily_key, monthly_key,
            tostring(tps), tostring(burst), tostring(now_ms),
            tostring(daily_quota), tostring(monthly_quota),
            tostring(daily_ttl), tostring(monthly_ttl))
    end

    -- quota_sha가 없거나, Redis가 "그 SHA로 로드된 스크립트 없음(NOSCRIPT)"을 반환하면
    -- (예: Redis 재시작으로 스크립트 캐시가 날아간 경우) 스크립트를 다시 로드하고 재시도.
    if not quota_sha or (rerr and tostring(rerr):find("NOSCRIPT", 1, true)) then
        local sha, lerr = red:script("load", script)
        if not sha then
            common.redis_keepalive(red)
            core.log.error("pat-quota: SCRIPT LOAD failed: ", lerr)
            return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "quota script load failed")
        end
        quota_sha = sha
        result, rerr = red:evalsha(quota_sha, 3, rl_key, daily_key, monthly_key,
            tostring(tps), tostring(burst), tostring(now_ms),
            tostring(daily_quota), tostring(monthly_quota),
            tostring(daily_ttl), tostring(monthly_ttl))
    end

    common.redis_keepalive(red)

    if not result or result == ngx.null then
        core.log.error("pat-quota: EVALSHA failed: ", rerr)
        return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "quota evaluation failed")
    end

    -- quota_deduct.lua는 {allowed(0/1), reason, remaining_daily} 3요소 배열을 반환
    local allowed = tonumber(result[1])
    local reason = result[2]
    local remaining = tonumber(result[3]) or 0

    if allowed ~= 1 then
        -- 거부 사유별로 적절한 Retry-After 값을 계산해 429로 응답.
        -- RATE_LIMIT(초당 한도 초과)는 1초 후 재시도, 일간/월간 쿼터 초과는 리셋 시점까지 대기.
        local retry_after = 1
        if reason == "DAILY_QUOTA" then
            retry_after = seconds_until_midnight_utc()
        elseif reason == "MONTHLY_QUOTA" then
            retry_after = seconds_until_month_end_utc()
        end
        return common.problem_json(ctx, 429, "CDP-1004", "Quota exceeded",
            "Quota exceeded: " .. tostring(reason),
            { ["Retry-After"] = retry_after })
    end

    -- 통과 시 클라이언트가 자신의 쿼터 상태를 알 수 있도록 표준 RateLimit 헤더를 응답에 부착
    -- (IETF draft RateLimit-* 헤더 관례)
    local reset_ts = ngx.time() + seconds_until_midnight_utc()
    core.response.set_header("X-RateLimit-Limit", tostring(daily_quota))
    core.response.set_header("X-RateLimit-Remaining", tostring(remaining))
    core.response.set_header("X-RateLimit-Reset", tostring(reset_ts))
    core.response.set_header("X-RateLimit-Policy",
        tostring(tps) .. ";w=1, " .. tostring(daily_quota) .. ";w=86400, " .. tostring(monthly_quota) .. ";w=2592000")
end

return _M
```

**핵심 포인트**
- `quota_deduct.lua`를 `EVALSHA`로 실행해 rate limit + 일간/월간 쿼터를 **하나의 원자적 연산**으로 처리한다(레이스 컨디션 방지).
- `quota_sha`는 워커 프로세스 전역 변수로 캐시되며, Redis가 재시작되어 스크립트 캐시가 날아가면 `NOSCRIPT` 에러를 감지해 자동으로 재로드한다.
- 응답 헤더에 `X-RateLimit-*` 계열을 부착해 클라이언트가 자신의 쿼터 상태를 알 수 있게 한다.

---

## 4. `plugins/apisix/plugins/pat-token-exchange.lua` — PAT → 내부 JWT 교환 플러그인

**실행 시점**: `access` phase, priority `3000` (pat-quota 다음, 가장 마지막에 실행되는 커스텀 access 플러그인).
**역할**: 외부에 노출된 PAT를 내부 시스템이 이해하는 JWT로 교환하고(Redis 캐시 우선, 미스 시 Token Exchange Service 호출), 클라이언트가 위조할 수 있는 헤더를 제거한 뒤 신뢰 가능한 시민 개발자 식별 헤더를 주입한다.

```lua
-- pat-token-exchange — Citizen Developer Portal gateway plugin.
-- Implements spec section 6.4: Redis-cached JWT lookup, on-miss call to the
-- Token Exchange Service, Authorization header swap (PAT -> internal JWT),
-- citizen headers injection, and stripping of client-spoofable headers.
-- Runs in the `access` phase, after pat-quota, still ahead of proxy-rewrite.
local core   = require("apisix.core")
local http   = require("resty.http")
local cjson  = require("cjson.safe")
local common = require("citizen.common")

local ngx = ngx

local schema = {
    type = "object",
    properties = {
        redis_host = { type = "string", default = "redis" },
        redis_port = { type = "integer", default = 6379 },
        redis_password = { type = "string", default = "" },
        txs_url = { type = "string" },  -- Token Exchange Service base URL
        internal_api_key = { type = "string", default = "" },
    },
    required = { "txs_url" },
}

local _M = {
    version = 0.1,
    priority = 3000,
    name = "pat-token-exchange",
    schema = schema,
}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

-- 부여된 scope 목록을 정렬 후 이어붙여 해시(앞 8자)를 만든다.
-- 같은 PAT라도 요청마다 scope 순서가 달라질 수 있으므로 정렬해서 캐시 키를 안정화하고,
-- scope 조합이 달라지면 캐시 키도 달라지도록 해 잘못된 캐시된 JWT를 재사용하지 않게 한다.
local function scope_hash(scopes)
    local copy = {}
    for i, s in ipairs(scopes) do
        copy[i] = s
    end
    table.sort(copy)
    return common.sha256_hex(table.concat(copy, " ")):sub(1, 8)
end

-- 클라이언트가 직접 보냈을 수 있는 "신뢰 헤더"를 요청에서 제거.
-- 이 헤더들은 뒤에서 이 플러그인 자신이 다시 설정할 값들이므로, 외부에서 미리
-- 심어 넣어 업스트림을 속이는(헤더 스푸핑/권한 상승) 것을 막기 위한 방어 로직이다.
local function strip_spoofable_headers(ctx)
    local headers = core.request.headers(ctx)
    for name, _ in pairs(headers) do
        local lname = name:lower()
        if lname:find("^x%-citizen%-") or lname == "x-forwarded-user"
            or lname == "x-user-sub" or lname == "cookie" then
            core.request.set_header(ctx, name, nil)
        end
    end
end

function _M.access(conf, ctx)
    local token_id = ctx.cdp_token_id
    local user_sub = ctx.cdp_user_sub
    local scopes = ctx.cdp_scopes or {}

    if not token_id then
        return common.problem_json(ctx, 500, "CDP-9000", "Gateway misconfigured", "pat-token-exchange ran without pat-auth context")
    end

    -- 캐시 키: "cdp:jwt:{tokenId}:{scope해시}" — 같은 PAT라도 scope 조합이 바뀌면
    -- 다른 캐시 엔트리를 사용하도록 분리
    local sh = scope_hash(scopes)
    local cache_key = "cdp:jwt:" .. token_id .. ":" .. sh

    local red, cerr = common.redis_connect(conf.redis_host, conf.redis_port, conf.redis_password)
    if not red then
        core.log.error("pat-token-exchange: redis connect failed: ", cerr)
        return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "redis unavailable")
    end

    -- 1) Redis에 이미 교환된 JWT가 캐시되어 있는지 조회
    local jwt, gerr = red:get(cache_key)
    if gerr then
        core.log.warn("pat-token-exchange: redis get failed: ", gerr)
        jwt = nil
    end
    if jwt == ngx.null then
        jwt = nil
    end
    common.redis_keepalive(red)

    if not jwt then
        -- 2) 캐시 미스 → Token Exchange Service(TXS)에 새 JWT 발급 요청
        local headers = {
            ["Content-Type"] = "application/json",
            ["Accept"] = "application/json",
        }
        if conf.internal_api_key and conf.internal_api_key ~= "" then
            headers["X-Internal-Key"] = conf.internal_api_key
        end

        local httpc = http.new()
        httpc:set_timeout(3000)
        local res, rerr = httpc:request_uri(conf.txs_url .. "/internal/token-exchange", {
            method = "POST",
            body = cjson.encode({ tokenId = token_id, userSub = user_sub, scopes = scopes }),
            headers = headers,
        })

        if not res then
            core.log.error("pat-token-exchange: TXS call failed: ", rerr)
            return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", tostring(rerr))
        end
        if res.status ~= 200 then
            core.log.error("pat-token-exchange: TXS returned ", res.status, ": ", res.body)
            return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable",
                "Token exchange failed with status " .. res.status)
        end

        local data, jerr = cjson.decode(res.body)
        if not data or not data.accessToken then
            core.log.error("pat-token-exchange: bad TXS response body: ", jerr)
            return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "invalid token-exchange response")
        end
        jwt = data.accessToken
        -- 주의: 이 플러그인은 새로 발급받은 JWT를 Redis에 다시 쓰지 않는다.
        -- 캐시 적재(SET)는 TXS(token-exchange-service) 쪽 책임으로 분리되어 있는 것으로 보인다.
    end

    ctx.cdp_jwt = jwt
    -- 3) 클라이언트가 심었을 수 있는 신뢰 헤더 제거 → 4) 검증된 값으로 재주입
    strip_spoofable_headers(ctx)

    core.request.set_header(ctx, "Authorization", "Bearer " .. jwt)     -- 외부 PAT를 내부 JWT로 교체
    core.request.set_header(ctx, "X-Citizen-PAT-Id", token_id)          -- 업스트림이 참조할 PAT 식별자
    core.request.set_header(ctx, "X-Citizen-Channel", "citizen")        -- 이 요청이 시민개발자 채널 경유임을 표시
    core.request.set_header(ctx, "X-Request-Id", ctx.cdp_trace_id)      -- 업스트림 로그와 상관관계 추적용

    core.response.set_header("X-Request-Id", ctx.cdp_trace_id)          -- 클라이언트도 같은 trace id를 받아 문의 시 활용 가능
end

return _M
```

**핵심 포인트**
- Redis 캐시(`cdp:jwt:{tokenId}:{scopeHash}`)를 먼저 조회하고, 없을 때만 Token Exchange Service를 호출해 지연시간을 줄인다.
- `scope_hash`로 scope 조합이 다르면 별도 캐시 엔트리를 쓰게 해, 잘못된 권한 범위의 JWT가 재사용되지 않도록 한다.
- `strip_spoofable_headers`는 클라이언트가 `X-Citizen-*`, `X-User-Sub`, `Cookie` 같은 헤더를 미리 심어 업스트림을 속이는 것을 막는 보안 장치다.

---

## 5. `plugins/apisix/plugins/pat-audit.lua` — 감사 로그 플러그인

**실행 시점**: `log` phase, priority `100`. `log` phase는 앞 단계에서 `core.response.exit`(즉, `problem_json` 반환)으로 요청이 조기 종료되었더라도 항상 실행되므로, 실패 트래픽까지 빠짐없이 감사 로그로 남길 수 있다.
**역할**: 요청 1건마다 감사 이벤트를 워커별 메모리 배치에 쌓아두고, 배치 크기 또는 주기적 타이머 중 먼저 도달하는 조건에 맞춰 portal-backend로 비동기 전송한다.

```lua
-- pat-audit — Citizen Developer Portal gateway plugin.
-- Implements spec section 6.5: async batched audit log shipping to
-- portal-backend's `/internal/audit`, with PAT-plaintext masking. Runs in
-- the `log` phase, which fires even when an earlier phase (pat-auth /
-- pat-quota / pat-token-exchange) exited the request early via
-- core.response.exit — that's exactly the failure traffic we need audited.
local core   = require("apisix.core")
local http   = require("resty.http")
local cjson  = require("cjson.safe")
local common = require("citizen.common")

local ngx = ngx

local schema = {
    type = "object",
    properties = {
        portal_backend_url = { type = "string" },
        internal_api_key = { type = "string", default = "" },
        audit_batch_size = { type = "integer", default = 100 },     -- 이 개수가 쌓이면 즉시 전송
        audit_flush_interval_s = { type = "number", default = 5 },  -- 이 주기마다 강제 전송
    },
    required = { "portal_backend_url" },
}

local _M = {
    version = 0.1,
    priority = 100,
    name = "pat-audit",
    schema = schema,
}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

-- Per-worker batch state (mirrors the previous FastAPI gateway's in-process
-- batching — each worker ships its own batch independently).
-- 워커 프로세스 전역(모듈 레벨) 상태. nginx/APISIX는 워커마다 별도의 Lua VM을
-- 가지므로, 이 배치는 "워커 단위"로 독립적으로 쌓이고 전송된다(워커 간 공유 없음).
local _batch = {}
local _timer_started = false

-- 배치를 실제로 portal-backend의 /internal/audit 엔드포인트에 POST.
local function send_batch(portal_backend_url, internal_api_key, entries)
    if #entries == 0 then
        return
    end
    local headers = { ["Content-Type"] = "application/json" }
    if internal_api_key and internal_api_key ~= "" then
        headers["X-Internal-Key"] = internal_api_key
    end

    local httpc = http.new()
    httpc:set_timeout(5000)
    local res, err = httpc:request_uri(portal_backend_url .. "/internal/audit", {
        method = "POST",
        body = cjson.encode(entries),
        headers = headers,
    })
    if not res then
        -- 전송 실패 시 재시도하지 않고 버린다(best-effort 로깅). 감사 로그 유실을
        -- 감수하더라도 게이트웨이의 요청 처리 경로를 막지 않는 것을 우선한다.
        core.log.warn("pat-audit: flush failed: ", err, " (dropped ", #entries, " events)")
    elseif res.status >= 300 then
        core.log.warn("pat-audit: flush got status ", res.status, " (", #entries, " events)")
    end
end

-- Handing the buffer off must stay yield-free, otherwise a concurrent log()
-- could append to a table that has already been taken.
-- 현재 배치 테이블을 통째로 꺼내고, 그 자리에 새 빈 테이블을 즉시 채워 넣는다.
-- 이 함수는 절대 코루틴 yield를 일으키면 안 된다(예: 소켓 I/O 호출 금지) —
-- 만약 yield가 일어나면 그 사이에 다른 요청의 log()가 "아직 안 비워진" _batch에
-- 계속 append할 수 있고, 그러면 그 항목들이 유실되거나 중복 전송될 수 있다.
local function take_batch()
    local to_send = _batch
    _batch = {}
    return to_send
end

-- ngx.timer.at(0, ...)으로 예약되는 즉시 플러시 콜백(배치 크기 트리거용)
local function flush_now(premature, portal_backend_url, internal_api_key, entries)
    if premature then
        return
    end
    send_batch(portal_backend_url, internal_api_key, entries)
end

local periodic_flush  -- 아래에서 정의되는 함수를 미리 전방 선언(재귀적 자기 예약을 위해)

-- Self-rescheduling timer chain, the same pattern APISIX's own
-- utils/batch-processor.lua uses. ngx.timer.every would also fire
-- periodically but never reports `premature`, so anything still buffered
-- when the worker exits would be lost — which is exactly the failure mode
-- this replaced (events sat in per-worker batches until the *next* request
-- on that worker, and vanished on restart).
-- `ngx.timer.every`가 아니라 `ngx.timer.at`을 매번 재귀적으로 예약하는 이유:
-- `ngx.timer.every`는 워커 종료 시 `premature=true`로 콜백을 호출해주지 않아서
-- "워커가 종료되는 순간 아직 안 보낸 배치가 통째로 유실"되는 문제가 있었다.
-- `ngx.timer.at`을 스스로 재예약하는 체인 방식은 매 콜백마다 premature 값을
-- 받을 수 있어, 워커 종료 시점에도 마지막으로 한 번 더 flush를 시도할 수 있다.
local function schedule_periodic_flush(interval, portal_backend_url, internal_api_key)
    local ok, err = ngx.timer.at(interval, periodic_flush, interval,
                                 portal_backend_url, internal_api_key)
    if not ok then
        core.log.error("pat-audit: failed to schedule periodic flush: ", err)
        _timer_started = false  -- let the next log() retry registration
    end
end

-- 주기적 타이머 콜백 본체. premature=true는 "워커가 지금 종료되는 중"이라는 신호.
function periodic_flush(premature, interval, portal_backend_url, internal_api_key)
    local entries = take_batch()
    if #entries > 0 then
        -- Best effort on worker exit: cosockets are usually unavailable in
        -- that context so this may only log a failure, but that still beats
        -- dropping the buffer silently.
        -- 워커 종료 중에는 cosocket이 막혀 있을 수 있어 이 전송이 실패할 수도 있지만,
        -- 시도조차 안 하는 것보다는 낫다는 판단.
        send_batch(portal_backend_url, internal_api_key, entries)
    end
    if premature then
        return  -- 워커 종료 중이면 다음 타이머를 재예약하지 않고 끝낸다
    end
    schedule_periodic_flush(interval, portal_backend_url, internal_api_key)
end

-- 게이트웨이 에러 코드를 감사 이벤트 유형으로 분류.
-- (error_code가 없으면 = 정상 처리된 API 호출)
local function classify_event(error_code)
    if not error_code then
        return "API_CALL"
    elseif error_code == "CDP-1001" or error_code == "CDP-1002" then
        return "AUTH_FAILED"
    elseif error_code == "CDP-1003" or error_code == "CDP-1006" then
        return "SCOPE_DENIED"
    elseif error_code == "CDP-1004" then
        return "QUOTA_EXCEEDED"
    elseif error_code == "CDP-2001" then
        return "EXCHANGE_FAILED"
    end
    return "ERROR"
end

-- APISIX log phase 진입점. 이 phase는 응답이 이미 클라이언트로 나간 뒤에도 실행되므로
-- 클라이언트 응답 지연에 영향을 주지 않으면서 로깅 작업을 할 수 있다.
function _M.log(conf, ctx)
    local latency_ms
    local rt = tonumber(ctx.var.request_time)
    if rt then
        latency_ms = math.floor(rt * 1000)
    end

    -- 앞선 3개 플러그인이 ctx에 심어둔 값들 + nginx 변수들을 모아 감사 이벤트 1건을 구성
    local entry = {
        event_type = classify_event(ctx.cdp_error_code),
        token_id = ctx.cdp_token_id,
        user_sub = ctx.cdp_user_sub,
        jwt_jti = common.extract_jwt_jti(ctx.cdp_jwt),
        trace_id = ctx.cdp_trace_id,
        client_ip = ctx.cdp_client_ip or ctx.var.remote_addr,
        http_method = ctx.var.request_method,
        request_path = common.mask_pat(ctx.var.request_uri),  -- URL에 PAT이 섞여 들어간 경우까지 대비해 마스킹
        status_code = ngx.status,
        latency_ms = latency_ms,
        error_code = ctx.cdp_error_code,
    }

    table.insert(_batch, entry)

    -- The periodic timer is per worker, not per route, so it keeps whichever
    -- route's endpoint/key/interval registered it first. Those are
    -- gateway-wide settings in practice (every route gets the same values
    -- from apisix_client._build_route).
    -- 주기적 타이머는 "워커당 1개"만 존재해야 하므로, 이 워커에서 아직 시작되지
    -- 않았을 때만 등록한다. 여러 라우트가 이 플러그인을 쓰더라도 실제로는 모두
    -- 같은 portal_backend_url/interval 설정을 쓰기 때문에 문제가 되지 않는다.
    if not _timer_started then
        _timer_started = true
        schedule_periodic_flush(conf.audit_flush_interval_s or 5,
                                conf.portal_backend_url, conf.internal_api_key)
    end

    -- Size trigger only: the timer owns time-based flushing now.
    -- 배치가 설정된 크기에 도달하면 시간 트리거를 기다리지 않고 즉시(0초 뒤) 전송 예약.
    if #_batch >= (conf.audit_batch_size or 100) then
        local ok, terr = ngx.timer.at(0, flush_now, conf.portal_backend_url,
                                      conf.internal_api_key, take_batch())
        if not ok then
            core.log.warn("pat-audit: failed to schedule flush timer: ", terr)
        end
    end
end

return _M
```

**핵심 포인트**
- `log` phase에서 실행되므로 클라이언트 응답 지연과 무관하며, 앞단에서 조기 종료(에러 응답)된 요청도 빠짐없이 캡처한다.
- 배치 전송은 "크기 트리거"(`audit_batch_size`)와 "시간 트리거"(`audit_flush_interval_s`) 두 가지로 이루어지며, `ngx.timer.at`을 재귀적으로 재예약하는 체인 방식을 써서 워커 종료 시에도 마지막 flush 기회를 갖는다.
- `mask_pat`으로 URL에 PAT 원문이 포함된 경우까지 대비해 감사 로그에 시크릿이 남지 않도록 한다.

---

## 6. `infra/redis/quota_deduct.lua` — Redis 서버 측 원자적 쿼터 스크립트

**역할**: `pat-quota.lua`가 `EVALSHA`로 호출하는 Redis Lua 스크립트. 토큰버킷(초당 rate limit) 검사, 일간/월간 쿼터 검사, 통과 시 차감까지를 Redis 서버 안에서 **하나의 원자적 트랜잭션**으로 처리한다(Redis는 스크립트 실행 중 다른 명령을 끼워넣지 않으므로 동시 요청 간 레이스 컨디션이 없다).

```lua
-- KEYS[1]=cdp:rl:{tokenId}  KEYS[2]=cdp:quota:d:{tokenId}:{yyyyMMdd}  KEYS[3]=cdp:quota:m:{tokenId}:{yyyyMM}
-- ARGV[1]=tps ARGV[2]=burst ARGV[3]=now_ms ARGV[4]=dailyQuota ARGV[5]=monthlyQuota
-- ARGV[6]=dailyTtl ARGV[7]=monthlyTtl
-- Returns: {allowed:int, reason:string, remaining_daily:int}

-- 1) 토큰버킷 상태 조회: 이전에 저장해둔 토큰 잔량(tokens)과 마지막 갱신 시각(ts)
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local ts     = tonumber(redis.call('HGET', KEYS[1], 'ts'))
local tps    = tonumber(ARGV[1])   -- 초당 리필 속도(허용 TPS)
local burst  = tonumber(ARGV[2])   -- 버킷 최대 용량(순간 버스트 허용치)
local now    = tonumber(ARGV[3])   -- 현재 시각(ms)

-- 2) 최초 호출(해당 키가 아직 없음)이면 버킷을 가득 찬 상태로 초기화
if tokens == nil then tokens = burst; ts = now end

-- 3) 마지막 갱신 이후 경과 시간(초) * tps 만큼 토큰을 리필. burst를 넘지 않도록 min으로 캡.
local refill = (now - ts) / 1000 * tps
tokens = math.min(burst, tokens + refill)

-- 4) 사용 가능한 토큰이 1 미만이면 rate limit 초과 → 즉시 거부(쿼터 검사도 하지 않고 조기 반환)
if tokens < 1 then
  redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
  redis.call('EXPIRE', KEYS[1], 60)  -- 오래 미사용된 rate-limit 키는 60초 뒤 자동 소멸(메모리 누수 방지)
  return {0, 'RATE_LIMIT', 0}
end

-- 5) 일간 쿼터 검사: 오늘 누적 호출 수가 dailyQuota 이상이면 거부
local d = tonumber(redis.call('GET', KEYS[2]) or '0')
if d >= tonumber(ARGV[4]) then return {0, 'DAILY_QUOTA', 0} end

-- 6) 월간 쿼터 검사: 이번 달 누적 호출 수가 monthlyQuota 이상이면 거부
local m = tonumber(redis.call('GET', KEYS[3]) or '0')
if m >= tonumber(ARGV[5]) then return {0, 'MONTHLY_QUOTA', 0} end

-- 7) 여기까지 왔다면 모든 검사 통과 → 실제 차감 수행
tokens = tokens - 1
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], 60)
d = redis.call('INCR', KEYS[2]); redis.call('EXPIRE', KEYS[2], tonumber(ARGV[6]))  -- 일간 카운터 +1, TTL 갱신
m = redis.call('INCR', KEYS[3]); redis.call('EXPIRE', KEYS[3], tonumber(ARGV[7]))  -- 월간 카운터 +1, TTL 갱신

-- 8) 허용 + 사유 없음(OK) + 오늘 남은 일간 쿼터 반환
return {1, 'OK', tonumber(ARGV[4]) - d}
```

**핵심 포인트**
- **토큰버킷 알고리즘**: `tokens`(현재 잔량)와 `ts`(마지막 갱신 시각)만 저장해두고, 호출 시점마다 경과 시간 기반으로 리필량을 계산하는 "lazy refill" 방식 — 별도의 백그라운드 리필 프로세스가 필요 없다.
- **검사 순서**: rate limit(초당) → 일간 쿼터 → 월간 쿼터 순으로, 앞에서 걸리면 뒤의 검사와 차감을 전혀 수행하지 않는다.
- **원자성**: Redis 스크립트는 단일 스레드에서 다른 명령과 인터리빙 없이 실행되므로, "조회 후 차감" 사이에 동시 요청이 끼어들어 쿼터를 초과 소비하는 TOCTOU(Time-Of-Check-Time-Of-Use) 문제가 원천 차단된다.
- 모든 키에 `EXPIRE`를 걸어 자동 소멸시키므로, 오래된 토큰/사용되지 않는 날짜·월 키가 Redis에 영구적으로 쌓이지 않는다.

---

## 7. `infra/redis/unlock_if_mine.lua` — Redis 분산 락 안전 해제 스크립트

**역할**: 게이트웨이 플러그인이 아니라 `services/token-exchange-service/redis_client.py`가 사용하는 Redis Lua 스크립트. "내가 건 락이 맞을 때만 해제한다"는 **compare-and-delete** 패턴을 원자적으로 구현한다.

```lua
-- KEYS[1] = 락으로 사용 중인 Redis 키
-- ARGV[1] = 이 락을 획득할 때 내가 세팅했던 고유 토큰 값(예: 요청/프로세스 식별자)

-- 1) 현재 락 값이 내가 세팅한 값과 정확히 같은지 확인
if redis.call("GET", KEYS[1]) == ARGV[1] then
  -- 2) 같다면(=내가 아직도 락 소유자다) 안전하게 삭제
  return redis.call("DEL", KEYS[1])
else
  -- 3) 다르다면(=락이 이미 만료되어 다른 프로세스가 새로 획득했거나, 애초에 내 락이 아님)
  --    아무것도 하지 않고 0 반환 — 절대 남의 락을 지우지 않는다
  return 0
end
```

**핵심 포인트**
- 분산 락(예: `SET key value NX PX ttl`으로 획득)을 해제할 때 단순히 `DEL key`만 호출하면, 내 락이 TTL로 이미 만료되고 그 사이 다른 프로세스가 같은 키로 새 락을 잡은 경우 **그 다른 프로세스의 락을 실수로 지워버리는 버그**가 생길 수 있다.
- 이 스크립트는 "GET으로 비교 → 일치할 때만 DEL"을 Redis 서버 안에서 원자적으로 수행해, 그 사이에 다른 클라이언트가 끼어들 여지를 없앤다(GET과 DEL을 애플리케이션 코드에서 따로 호출하면 그 사이에 락이 만료·재획득될 수 있어 원자성이 깨진다).
- `token-exchange-service`가 (아마도 동시에 같은 PAT에 대해 중복으로 Token Exchange Service를 호출하지 않도록) 락을 걸고 작업 후 안전하게 해제하는 용도로 사용하는 것으로 보인다.

---

## 전체적인 설계 관찰

1. **관심사 분리**: 인증(pat-auth) / 속도제한·쿼터(pat-quota) / 토큰 교환(pat-token-exchange) / 감사(pat-audit)가 명확히 분리된 4개의 독립 플러그인이며, `common.lua`가 암호화·Redis·에러응답 등 공통 관심사를 모아 중복을 줄인다.
2. **캐시-우선, 원본 폴백**: PAT 메타데이터(Redis → portal-backend)와 JWT(Redis → Token Exchange Service) 모두 "Redis 캐시 우선 조회, 미스 시 원본 서비스 호출" 패턴을 일관되게 사용해 지연시간과 원본 서비스 부하를 줄인다.
3. **원자성은 Redis 스크립트로 보장**: rate limit/쿼터 차감(`quota_deduct.lua`)과 분산 락 해제(`unlock_if_mine.lua`) 둘 다, "여러 단계 검사+수정"이 필요한 로직을 애플리케이션 레이어가 아닌 Redis 서버 내 Lua 스크립트로 원자화해 레이스 컨디션을 원천 차단한다.
4. **보안 민감 처리 일원화**: HMAC 비교(`constant_time_eq`), PAT 마스킹(`mask_pat`)처럼 잘못 구현하면 취약점이 되는 로직을 `common.lua` 한 곳에 모아, 4개 플러그인이 각자 재구현하다가 실수하는 것을 방지한다.
5. **실패까지 놓치지 않는 감사**: `pat-audit`이 `log` phase에서 동작해, 앞 단계 어디서 요청이 거부되었든(인증 실패, 쿼터 초과 등) 관계없이 모든 트래픽이 감사 로그로 남는다.
