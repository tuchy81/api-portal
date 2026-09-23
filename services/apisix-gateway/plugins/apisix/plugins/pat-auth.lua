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

local schema = {
    type = "object",
    properties = {
        redis_host = { type = "string", default = "redis" },
        redis_port = { type = "integer", default = 6379 },
        redis_password = { type = "string", default = "" },
        server_key = { type = "string" },
        portal_backend_url = { type = "string" },
        internal_api_key = { type = "string", default = "" },
        neg_cache_ttl = { type = "integer", default = 30 },
        -- HTTP method -> ordered list of {pattern, scope}, most-specific
        -- pattern first. `pattern` is an anchored PCRE built from the
        -- scope's path_pattern (portal-backend's _path_pattern_to_regex),
        -- so a route whose `uri`/`uris` cover a whole resource tree (e.g.
        -- `/capi/v1/vendors/*`) can still require different scopes for
        -- `/vendors` vs `/vendors/{id}` under the same method.
        required_scope_map = {
            type = "object",
            minProperties = 1,
            additionalProperties = {
                type = "array",
                minItems = 1,
                items = {
                    type = "object",
                    properties = {
                        pattern = { type = "string" },
                        scope = { type = "string" },
                    },
                    required = { "pattern", "scope" },
                },
            },
        },
    },
    required = { "server_key", "portal_backend_url", "required_scope_map" },
}

local _M = {
    version = 0.1,
    priority = 3010,
    name = "pat-auth",
    schema = schema,
}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

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
local function to_int(a, b, c, d)
    return bit.bor(
        bit.lshift(tonumber(a), 24),
        bit.lshift(tonumber(b), 16),
        bit.lshift(tonumber(c), 8),
        tonumber(d)
    )
end

local function cidr_mask(bits)
    if bits <= 0 then
        return 0
    end
    if bits >= 32 then
        return bit.bnot(0)
    end
    return bit.lshift(bit.bnot(0), 32 - bits)
end

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
    return bit.band(ip_int, mask) == bit.band(net_int, mask)
end

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
local function now_iso_utc()
    return ngx.utctime():gsub(" ", "T") .. "+00:00"
end

-- Fallback: ask portal-backend for PAT metadata on a Redis cache miss.
-- Mirrors plugins/pat_auth.py's behaviour, including the fact that HMAC
-- verification is skipped for this path (no HMAC ships in the fallback
-- payload — only Redis, populated at issuance time, has it).
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
    return {
        sub = data.sub,
        scopes = cjson.encode(data.scopes or {}),
        status = data.status,
        cidr = cjson.encode(data.cidr or {}),
        hash = "",
        rate_limit_tps = tostring((data.quota and data.quota.rateLimitTps) or 10),
        burst = tostring((data.quota and data.quota.burst) or 20),
        daily_quota = tostring((data.quota and data.quota.dailyQuota) or 5000),
        monthly_quota = tostring((data.quota and data.quota.monthlyQuota) or 100000),
        expires_at = data.expiresAt or "",
    }, nil
end

function _M.rewrite(conf, ctx)
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
    ctx.cdp_token_id = token_id
    ctx.cdp_client_ip = get_client_ip(ctx)
    ctx.cdp_trace_id = common.gen_trace_id()

    local red, rerr = common.redis_connect(conf.redis_host, conf.redis_port, conf.redis_password)
    if not red then
        core.log.error("pat-auth: redis connect failed: ", rerr)
        return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "redis unavailable")
    end

    local res, err = red:hgetall("cdp:pat:" .. token_id)
    if err then
        common.redis_keepalive(red)
        core.log.error("pat-auth: redis hgetall failed: ", err)
        return common.problem_json(ctx, 503, "CDP-2001", "Authorization service unavailable", "redis error")
    end
    local meta = common.hgetall_to_table(res)

    if not meta then
        local neg_key = "cdp:pat:neg:" .. token_id
        local neg, _ = red:get(neg_key)
        if neg and neg ~= ngx.null then
            common.redis_keepalive(red)
            return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "token not found (negative cache)")
        end

        local fetched, ferr = fetch_from_portal(conf, token_id)
        if not fetched then
            red:setex(neg_key, conf.neg_cache_ttl, "1")
            common.redis_keepalive(red)
            core.log.info("pat-auth: portal fallback miss for ", token_id, ": ", ferr)
            return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "token not found")
        end
        meta = fetched
    end

    -- Known from here on, so rejections below are attributable to a user too.
    ctx.cdp_user_sub = meta.sub

    if meta.status ~= "ACTIVE" then
        common.redis_keepalive(red)
        return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "Token status is " .. tostring(meta.status))
    end

    if meta.expires_at and meta.expires_at ~= "" and meta.expires_at < now_iso_utc() then
        common.redis_keepalive(red)
        return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "Token has expired")
    end

    if meta.hash and meta.hash ~= "" then
        local expected = common.hmac_sha256_hex(conf.server_key, secret)
        if not common.constant_time_eq(expected, meta.hash) then
            common.redis_keepalive(red)
            return common.problem_json(ctx, 401, "CDP-1001", "Invalid or revoked token", "Token signature mismatch")
        end
    end

    local client_ip = ctx.cdp_client_ip
    local allowed_cidr = cjson.decode(meta.cidr or "[]") or {}
    if not check_cidr(client_ip, allowed_cidr) then
        common.redis_keepalive(red)
        return common.problem_json(ctx, 403, "CDP-1006",
            "Client IP not allowed",
            "Client IP " .. client_ip .. " not in allowed CIDR")
    end

    -- `ctx.var.uri` is the normalized request path (no query string), which
    -- is what path_pattern regexes are anchored against.
    local method_patterns = conf.required_scope_map[ctx.var.request_method]
    local granted_scopes = cjson.decode(meta.scopes or "[]") or {}
    local has_scope = false
    local required_scope = nil
    if method_patterns then
        for _, candidate in ipairs(method_patterns) do
            if ngx.re.match(ctx.var.uri, candidate.pattern, "jo") then
                required_scope = candidate.scope
                break
            end
        end
        -- The method is registered for this route but no scope pattern
        -- matches this specific path: deny rather than silently falling
        -- through, so an unlisted sub-resource path can't slip past scope
        -- enforcement just because a sibling path_pattern happens to match
        -- the method.
        if required_scope then
            for _, s in ipairs(granted_scopes) do
                if s == required_scope then
                    has_scope = true
                    break
                end
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
    ctx.cdp_scopes = granted_scopes
    ctx.cdp_rate_limit_tps = tonumber(meta.rate_limit_tps) or 10
    ctx.cdp_burst = tonumber(meta.burst) or 20
    ctx.cdp_daily_quota = tonumber(meta.daily_quota) or 5000
    ctx.cdp_monthly_quota = tonumber(meta.monthly_quota) or 100000
end

return _M
