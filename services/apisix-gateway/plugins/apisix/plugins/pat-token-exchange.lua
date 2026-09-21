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

    core.response.set_header("X-Request-Id", ctx.cdp_trace_id)
end

return _M
