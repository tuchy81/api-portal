"""Circuit breaker backed by Redis state."""
import time, logging
import redis as redis_lib
from config import settings

logger = logging.getLogger("txs.circuit_breaker")

CB_KEY = "cdp:cb:txs"
CB_WINDOW_KEY = "cdp:cb:txs:window"

def is_circuit_open(r: redis_lib.Redis) -> bool:
    state = r.get(CB_KEY)
    return state in ("OPEN", "HALF_OPEN")

def record_success(r: redis_lib.Redis):
    r.lpush(CB_WINDOW_KEY, "OK")
    r.ltrim(CB_WINDOW_KEY, 0, settings.cb_window_size - 1)
    r.expire(CB_WINDOW_KEY, 300)
    _evaluate_window(r)

def record_failure(r: redis_lib.Redis):
    r.lpush(CB_WINDOW_KEY, "FAIL")
    r.ltrim(CB_WINDOW_KEY, 0, settings.cb_window_size - 1)
    r.expire(CB_WINDOW_KEY, 300)
    _evaluate_window(r)

def _evaluate_window(r: redis_lib.Redis):
    window = r.lrange(CB_WINDOW_KEY, 0, -1)
    if len(window) < 10:
        return
    fail_count = sum(1 for x in window if x == "FAIL")
    fail_rate = fail_count / len(window)
    if fail_rate >= settings.cb_failure_threshold:
        r.setex(CB_KEY, settings.cb_open_duration, "OPEN")
        logger.warning(f"Circuit breaker OPEN: failure rate {fail_rate:.0%}")
    else:
        r.delete(CB_KEY)
