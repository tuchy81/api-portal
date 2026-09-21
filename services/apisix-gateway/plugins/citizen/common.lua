-- Shared helpers for the citizen-gateway custom plugins (pat-auth, pat-quota,
-- pat-token-exchange, pat-audit). Kept in one place so the four plugins stay
-- thin and the security-sensitive bits (HMAC compare, problem+json shape,
-- PAT masking) have a single implementation.
local core   = require("apisix.core")
local redis  = require("resty.redis")
local sha256 = require("resty.sha256")
local resty_str = require("resty.string")
local bit    = require("bit")
local cjson  = require("cjson.safe")

local ngx = ngx
local str_char = string.char
local str_byte = string.byte
local str_rep  = string.rep

local _M = {}

_M.PAT_REGEX = [[^Bearer\s+hdpat_([A-Za-z0-9]{12})_([A-Za-z0-9_-]{43})$]]

-- ---------------------------------------------------------------------------
-- HMAC-SHA256 (hex), implemented on top of resty.sha256 so we don't depend
-- on resty.hmac being present in every APISIX build. Verified against
-- Python's hmac.new(key, msg, hashlib.sha256).hexdigest() during development.
-- ---------------------------------------------------------------------------
function _M.hmac_sha256_hex(key, msg)
    local blocksize = 64
    if #key > blocksize then
        local h = sha256:new()
        h:update(key)
        key = h:final()
    end
    if #key < blocksize then
        key = key .. str_rep(str_char(0), blocksize - #key)
    end

    local ipad, opad = {}, {}
    for i = 1, blocksize do
        local kb = str_byte(key, i)
        ipad[i] = str_char(bit.bxor(kb, 0x36))
        opad[i] = str_char(bit.bxor(kb, 0x5c))
    end
    ipad = table.concat(ipad)
    opad = table.concat(opad)

    local inner = sha256:new()
    inner:update(ipad)
    inner:update(msg)
    local inner_digest = inner:final()

    local outer = sha256:new()
    outer:update(opad)
    outer:update(inner_digest)
    return resty_str.to_hex(outer:final())
end

-- Constant-time compare (avoid leaking timing info on PAT secret checks).
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
local trace_seq = 0
function _M.gen_trace_id()
    trace_seq = trace_seq + 1
    local raw = ngx.now() .. "|" .. tostring(ngx.worker.pid()) .. "|" ..
                tostring(trace_seq) .. "|" .. tostring(math.random(1, 1e9))
    return _M.sha256_hex(raw):sub(1, 32)
end

-- ---------------------------------------------------------------------------
-- RFC 9457 problem+json helper. Returns (status, body_table) — hand the
-- result straight back from a plugin phase function, e.g.:
--   return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "...")
-- ---------------------------------------------------------------------------
function _M.problem_json(ctx, status, code, title, detail, extra_headers)
    if ctx then
        ctx.cdp_error_code = code
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
function _M.extract_jwt_jti(jwt)
    if not jwt then
        return nil
    end
    local payload_b64 = jwt:match("^[^.]+%.([^.]+)%.")
    if not payload_b64 then
        return nil
    end
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
