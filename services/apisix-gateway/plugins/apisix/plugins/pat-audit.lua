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
        audit_batch_size = { type = "integer", default = 100 },
        audit_flush_interval_s = { type = "number", default = 5 },
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
local _batch = {}
local _timer_started = false

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
        core.log.warn("pat-audit: flush failed: ", err, " (dropped ", #entries, " events)")
    elseif res.status >= 300 then
        core.log.warn("pat-audit: flush got status ", res.status, " (", #entries, " events)")
    end
end

-- Handing the buffer off must stay yield-free, otherwise a concurrent log()
-- could append to a table that has already been taken.
local function take_batch()
    local to_send = _batch
    _batch = {}
    return to_send
end

local function flush_now(premature, portal_backend_url, internal_api_key, entries)
    if premature then
        return
    end
    send_batch(portal_backend_url, internal_api_key, entries)
end

local periodic_flush

-- Self-rescheduling timer chain, the same pattern APISIX's own
-- utils/batch-processor.lua uses. ngx.timer.every would also fire
-- periodically but never reports `premature`, so anything still buffered
-- when the worker exits would be lost — which is exactly the failure mode
-- this replaced (events sat in per-worker batches until the *next* request
-- on that worker, and vanished on restart).
local function schedule_periodic_flush(interval, portal_backend_url, internal_api_key)
    local ok, err = ngx.timer.at(interval, periodic_flush, interval,
                                 portal_backend_url, internal_api_key)
    if not ok then
        core.log.error("pat-audit: failed to schedule periodic flush: ", err)
        _timer_started = false  -- let the next log() retry registration
    end
end

function periodic_flush(premature, interval, portal_backend_url, internal_api_key)
    local entries = take_batch()
    if #entries > 0 then
        -- Best effort on worker exit: cosockets are usually unavailable in
        -- that context so this may only log a failure, but that still beats
        -- dropping the buffer silently.
        send_batch(portal_backend_url, internal_api_key, entries)
    end
    if premature then
        return
    end
    schedule_periodic_flush(interval, portal_backend_url, internal_api_key)
end

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

function _M.log(conf, ctx)
    local latency_ms
    local rt = tonumber(ctx.var.request_time)
    if rt then
        latency_ms = math.floor(rt * 1000)
    end

    local entry = {
        event_type = classify_event(ctx.cdp_error_code),
        token_id = ctx.cdp_token_id,
        user_sub = ctx.cdp_user_sub,
        jwt_jti = common.extract_jwt_jti(ctx.cdp_jwt),
        trace_id = ctx.cdp_trace_id,
        client_ip = ctx.cdp_client_ip or ctx.var.remote_addr,
        http_method = ctx.var.request_method,
        request_path = common.mask_pat(ctx.var.request_uri),
        status_code = ngx.status,
        latency_ms = latency_ms,
        error_code = ctx.cdp_error_code,
    }

    table.insert(_batch, entry)

    -- The periodic timer is per worker, not per route, so it keeps whichever
    -- route's endpoint/key/interval registered it first. Those are
    -- gateway-wide settings in practice (every route gets the same values
    -- from apisix_client._build_route).
    if not _timer_started then
        _timer_started = true
        schedule_periodic_flush(conf.audit_flush_interval_s or 5,
                                conf.portal_backend_url, conf.internal_api_key)
    end

    -- Size trigger only: the timer owns time-based flushing now.
    if #_batch >= (conf.audit_batch_size or 100) then
        local ok, terr = ngx.timer.at(0, flush_now, conf.portal_backend_url,
                                      conf.internal_api_key, take_batch())
        if not ok then
            core.log.warn("pat-audit: failed to schedule flush timer: ", terr)
        end
    end
end

return _M
