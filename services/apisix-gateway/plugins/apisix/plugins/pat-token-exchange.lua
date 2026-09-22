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
        txs_url = { type = "string" },
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

local function scope_hash(scopes)
    local copy = {}
    for i, s in ipairs(scopes) do
        copy[i] = s
    end
    table.sort(copy)
    return common.sha256_hex(table.concat(copy, " ")):sub(1, 8)
end

-- Fixed claim -> upstream header mapping (HR/org context internal APIs
-- rely on). Only these claims are promoted to headers; anything else in the
-- JWT stays out of the request unless added here.
local CLAIM_HEADER_MAP = {
    user_id     = "X-USER-ID",
    company     = "X-COMPANY",
    org_cd      = "X-ORG-CD",
    asgn_cd     = "X-ASGN-CD",
    dept_cd     = "X-DEPT-CD",
    job_tit_cd  = "X-JOB-TIT-CD",
    offi_res_cd = "X-OFFI-RES-CD",
    user_origin = "X-USER-ORIGIN",
}

-- Lowercased lookup set so strip_spoofable_headers can block a client from
-- pre-seeding any of these before the gateway sets the real value.
local CLAIM_HEADER_NAMES_LOWER = {}
for _, header_name in pairs(CLAIM_HEADER_MAP) do
    CLAIM_HEADER_NAMES_LOWER[header_name:lower()] = true
end

local function strip_spoofable_headers(ctx)
    local headers = core.request.headers(ctx)
    for name, _ in pairs(headers) do
        local lname = name:lower()
        if lname:find("^x%-citizen%-") or lname == "x-forwarded-user"
            or lname == "x-user-sub" or lname == "cookie"
            or CLAIM_HEADER_NAMES_LOWER[lname] then
            core.request.set_header(ctx, name, nil)
        end
    end
end

-- Sets each mapped header from the exchanged JWT's claims. A claim that
-- isn't present in this particular JWT is simply left unset — the client's
-- own attempt to set it was already wiped by strip_spoofable_headers above,
-- so there's nothing to overwrite either way.
local function inject_claim_headers(ctx, jwt)
    local claims = common.decode_jwt_payload(jwt)
    if not claims then
        core.log.warn("pat-token-exchange: could not decode JWT payload for claim header injection")
        return
    end
    for claim_name, header_name in pairs(CLAIM_HEADER_MAP) do
        local value = claims[claim_name]
        if value ~= nil then
            local header_value
            if type(value) == "table" then
                header_value = cjson.encode(value)
            else
                header_value = tostring(value)
            end
            core.request.set_header(ctx, header_name, header_value)
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

    local sh = scope_hash(scopes)
    local cache_key = "cdp:jwt:" .. token_id .. ":" .. sh

    local red, cerr = common.redis_connect(conf.redis_host, conf.redis_port, conf.redis_password)
    if not red then
        core.log.error("pat-token-exchange: redis connect failed: ", cerr)
        return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "redis unavailable")
    end

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
    end

    ctx.cdp_jwt = jwt
    strip_spoofable_headers(ctx)

    core.request.set_header(ctx, "Authorization", "Bearer " .. jwt)
    core.request.set_header(ctx, "X-Citizen-PAT-Id", token_id)
    core.request.set_header(ctx, "X-Citizen-Channel", "citizen")
    core.request.set_header(ctx, "X-Request-Id", ctx.cdp_trace_id)
    inject_claim_headers(ctx, jwt)

    core.response.set_header("X-Request-Id", ctx.cdp_trace_id)
end

return _M
