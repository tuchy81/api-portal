"""Redis client for portal backend — PAT cache management."""
import json
import redis as redis_lib
from config import settings

_pool = None

def get_redis() -> redis_lib.Redis:
    global _pool
    if _pool is None:
        _pool = redis_lib.ConnectionPool(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            decode_responses=True,
            max_connections=10,
        )
    return redis_lib.Redis(connection_pool=_pool)

def cache_pat_meta(r: redis_lib.Redis, token_id: str, meta: dict, ttl_seconds: int):
    """Store PAT metadata hash in Redis. meta includes hmac, sub, scopes, status, cidr, quota."""
    key = f"cdp:pat:{token_id}"
    pipe = r.pipeline()
    pipe.hset(key, mapping={
        "hash": meta["hmac"],
        "sub": meta["sub"],
        "scopes": json.dumps(meta["scopes"]),
        "status": meta["status"],
        "cidr": json.dumps(meta.get("cidr", [])),
        "rate_limit_tps": str(meta.get("rate_limit_tps", 10)),
        "burst": str(meta.get("burst", 20)),
        "daily_quota": str(meta.get("daily_quota", 5000)),
        "monthly_quota": str(meta.get("monthly_quota", 100000)),
        "expires_at": meta.get("expires_at", ""),
    })
    if ttl_seconds > 0:
        pipe.expire(key, ttl_seconds)
    pipe.execute()

def invalidate_pat(r: redis_lib.Redis, token_id: str):
    """Delete PAT cache and all associated JWT cache keys."""
    pat_key = f"cdp:pat:{token_id}"
    idx_key = f"cdp:jwtidx:{token_id}"
    neg_key = f"cdp:pat:neg:{token_id}"

    jwt_keys = r.smembers(idx_key)
    pipe = r.pipeline()
    pipe.delete(pat_key)
    pipe.delete(neg_key)
    for jk in jwt_keys:
        pipe.delete(jk)
    pipe.delete(idx_key)
    pipe.execute()


def invalidate_jwt_cache(r: redis_lib.Redis, token_ids: list[str]) -> int:
    """Drop only the cached JWTs for the given PATs, leaving cdp:pat:* intact.

    Used when a change (API scope redefinition, Keycloak role update) means the
    next call must go through Token Exchange again to pick up the new scope
    set or claims, but the PAT itself remains valid. Returns the number of JWT
    cache entries deleted (best-effort — count reflects idx-set size before
    the pipeline runs)."""
    if not token_ids:
        return 0
    total = 0
    for token_id in token_ids:
        idx_key = f"cdp:jwtidx:{token_id}"
        jwt_keys = r.smembers(idx_key)
        if not jwt_keys:
            continue
        pipe = r.pipeline()
        for jk in jwt_keys:
            pipe.delete(jk)
        pipe.delete(idx_key)
        pipe.execute()
        total += len(jwt_keys)
    return total

def update_pat_status_in_redis(r: redis_lib.Redis, token_id: str, status: str):
    """Update just the status field in Redis PAT hash (e.g., ACTIVE → REVOKED)."""
    key = f"cdp:pat:{token_id}"
    if r.exists(key):
        r.hset(key, "status", status)

def update_pat_quota_in_redis(r: redis_lib.Redis, token_id: str, quota: dict):
    """Update quota fields in Redis PAT hash without full eviction."""
    key = f"cdp:pat:{token_id}"
    if r.exists(key):
        fields = {}
        if "rate_limit_tps" in quota:
            fields["rate_limit_tps"] = str(quota["rate_limit_tps"])
        if "burst" in quota:
            fields["burst"] = str(quota["burst"])
        if "daily_quota" in quota:
            fields["daily_quota"] = str(quota["daily_quota"])
        if "monthly_quota" in quota:
            fields["monthly_quota"] = str(quota["monthly_quota"])
        if fields:
            r.hset(key, mapping=fields)
