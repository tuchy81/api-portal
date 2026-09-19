"""Quota and rate-limit plugin — implements spec section 6.3 via Redis Lua script."""
import time, logging
from datetime import datetime, date
import redis as redis_lib
import redis_client as rc

logger = logging.getLogger("gateway.quota")

class QuotaError(Exception):
    def __init__(self, reason: str, retry_after: int = 1):
        self.reason = reason
        self.retry_after = retry_after

def _seconds_until_midnight() -> int:
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day) + __import__('datetime').timedelta(days=1)
    return int((midnight - now).total_seconds())

def _seconds_until_month_end() -> int:
    import calendar
    now = datetime.now()
    last_day = calendar.monthrange(now.year, now.month)[1]
    month_end = datetime(now.year, now.month, last_day, 23, 59, 59)
    return max(0, int((month_end - now).total_seconds()))

def deduct_quota(r: redis_lib.Redis, pat_ctx: dict) -> dict:
    """Execute atomic Lua quota deduction. Returns quota headers dict."""
    token_id = pat_ctx["token_id"]
    today = date.today().strftime("%Y%m%d")
    month = date.today().strftime("%Y%m")

    rl_key = f"cdp:rl:{token_id}"
    daily_key = f"cdp:quota:d:{token_id}:{today}"
    monthly_key = f"cdp:quota:m:{token_id}:{month}"

    tps = pat_ctx.get("rate_limit_tps", 10)
    burst = pat_ctx.get("burst", 20)
    daily_quota = pat_ctx.get("daily_quota", 5000)
    monthly_quota = pat_ctx.get("monthly_quota", 100000)
    now_ms = int(time.time() * 1000)
    daily_ttl = _seconds_until_midnight() + 300  # +5min buffer
    monthly_ttl = _seconds_until_month_end() + 300

    sha = rc.get_quota_sha()
    if sha:
        result = r.evalsha(
            sha, 3,
            rl_key, daily_key, monthly_key,
            str(tps), str(burst), str(now_ms),
            str(daily_quota), str(monthly_quota),
            str(daily_ttl), str(monthly_ttl)
        )
    else:
        # Fallback: load script and retry
        from pathlib import Path
        lua = (Path(__file__).parent.parent / "infra/redis/quota_deduct.lua").read_text()
        result = r.eval(
            lua, 3,
            rl_key, daily_key, monthly_key,
            str(tps), str(burst), str(now_ms),
            str(daily_quota), str(monthly_quota),
            str(daily_ttl), str(monthly_ttl)
        )

    allowed = int(result[0])
    reason = result[1]
    remaining = int(result[2]) if len(result) > 2 else 0

    if not allowed:
        if reason == "RATE_LIMIT":
            raise QuotaError("RATE_LIMIT", retry_after=1)
        elif reason == "DAILY_QUOTA":
            raise QuotaError("DAILY_QUOTA", retry_after=_seconds_until_midnight())
        else:
            raise QuotaError("MONTHLY_QUOTA", retry_after=_seconds_until_month_end())

    reset_ts = int(time.time()) + _seconds_until_midnight()
    return {
        "X-RateLimit-Limit": str(daily_quota),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_ts),
        "X-RateLimit-Policy": f"{tps};w=1, {daily_quota};w=86400, {monthly_quota};w=2592000",
    }
