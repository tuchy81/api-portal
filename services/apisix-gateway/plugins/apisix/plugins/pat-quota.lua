-- pat-quota — Citizen Developer Portal gateway plugin.
-- Implements spec section 6.3: atomic token-bucket + daily/monthly quota
-- deduction via the shared `infra/redis/quota_deduct.lua` script (single
-- source of truth, same file the old FastAPI gateway and this plugin both
-- EVALSHA). Runs in the `access` phase, right after pat-auth's `rewrite`.
local core   = require("apisix.core")
local common = require("citizen.common")

local QUOTA_SCRIPT_PATH = "/opt/redis-scripts/quota_deduct.lua"

local quota_script_text
local quota_sha -- cached per worker process; reloaded on NOSCRIPT

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

local function seconds_until_midnight_utc()
    local t = os.date("!*t")
    local elapsed = t.hour * 3600 + t.min * 60 + t.sec
    return 86400 - elapsed
end

local function is_leap(year)
    return (year % 4 == 0) and (year % 100 ~= 0 or year % 400 == 0)
end

local DAYS_IN_MONTH = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 }

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

function _M.access(conf, ctx)
    local token_id = ctx.cdp_token_id
    if not token_id then
        -- pat-auth should always run first and exit on failure; this would
        -- only happen if the route is misconfigured without pat-auth.
        return common.problem_json(ctx, 500, "CDP-9000", "Gateway misconfigured", "pat-quota ran without pat-auth context")
    end

    local today = os.date("!%Y%m%d")
    local month = os.date("!%Y%m")

    local rl_key = "cdp:rl:" .. token_id
    local daily_key = "cdp:quota:d:" .. token_id .. ":" .. today
    local monthly_key = "cdp:quota:m:" .. token_id .. ":" .. month

    local tps = ctx.cdp_rate_limit_tps
    local burst = ctx.cdp_burst
    local daily_quota = ctx.cdp_daily_quota
    local monthly_quota = ctx.cdp_monthly_quota
    local now_ms = math.floor(ngx.now() * 1000)
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

    local result, rerr
    if quota_sha then
        result, rerr = red:evalsha(quota_sha, 3, rl_key, daily_key, monthly_key,
            tostring(tps), tostring(burst), tostring(now_ms),
            tostring(daily_quota), tostring(monthly_quota),
            tostring(daily_ttl), tostring(monthly_ttl))
    end

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

    local allowed = tonumber(result[1])
    local reason = result[2]
    local remaining = tonumber(result[3]) or 0

    if allowed ~= 1 then
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

    local reset_ts = ngx.time() + seconds_until_midnight_utc()
    core.response.set_header("X-RateLimit-Limit", tostring(daily_quota))
    core.response.set_header("X-RateLimit-Remaining", tostring(remaining))
    core.response.set_header("X-RateLimit-Reset", tostring(reset_ts))
    core.response.set_header("X-RateLimit-Policy",
        tostring(tps) .. ";w=1, " .. tostring(daily_quota) .. ";w=86400, " .. tostring(monthly_quota) .. ";w=2592000")
end

return _M
