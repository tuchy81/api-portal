"""JWT cache with single-flight and probabilistic early expiration."""
import asyncio, hashlib, random, time, logging
import redis as redis_lib
from config import settings
import keycloak_client
import circuit_breaker as cb_module

logger = logging.getLogger("txs.cache")

UNLOCK_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
"""

def _scope_hash(scopes: list[str]) -> str:
    # 16 hex chars = 64 bits. Collisions are only meaningful within a single
    # tokenId (see _cache_key), but the previous 32-bit truncation was tight
    # enough that a token with many distinct scope combinations could hit one
    # accidentally. Must stay in lockstep with pat-token-exchange.lua's
    # scope_hash() — the gateway reads whichever key we wrote.
    return hashlib.sha256(" ".join(sorted(scopes)).encode()).hexdigest()[:16]

def _cache_key(token_id: str, scope_hash: str) -> str:
    return f"cdp:jwt:{token_id}:{scope_hash}"

def _lock_key(token_id: str, scope_hash: str) -> str:
    return f"cdp:lock:jwt:{token_id}:{scope_hash}"

def _idx_key(token_id: str) -> str:
    return f"cdp:jwtidx:{token_id}"

async def get_or_exchange_jwt(
    token_id: str,
    user_sub: str,
    scopes: list[str],
    r: redis_lib.Redis,
    node_id: str,
) -> tuple[str, bool]:
    """Returns (jwt_string, was_cached)."""
    sh = _scope_hash(scopes)
    ckey = _cache_key(token_id, sh)
    lkey = _lock_key(token_id, sh)

    # 1. Check cache
    jwt = r.get(ckey)
    if jwt:
        ttl = r.ttl(ckey)
        # Probabilistic early expiration (< 20% of TTL remaining)
        if ttl > 0 and ttl < settings.jwt_cache_ttl * 0.20:
            p = (settings.jwt_cache_ttl * 0.20 - ttl) / (settings.jwt_cache_ttl * 0.20)
            if random.random() < p:
                logger.debug(f"Probabilistic early refresh for {token_id} (TTL={ttl}s)")
                # Trigger refresh but return current JWT
                asyncio.create_task(_refresh_jwt(token_id, user_sub, scopes, r, node_id))
        return jwt, True

    # 2. Cache miss — try to acquire lock (single-flight)
    lock_acquired = r.set(lkey, node_id, nx=True, px=5000)

    if lock_acquired:
        try:
            result = await keycloak_client.exchange_token(user_sub, scopes)
            jwt = result["access_token"]
            r.setex(ckey, settings.jwt_cache_ttl, jwt)
            r.sadd(_idx_key(token_id), ckey)
            r.expire(_idx_key(token_id), settings.jwt_ttl)
            return jwt, False
        except Exception as e:
            logger.error(f"Token exchange failed for {token_id}: {e}")
            raise
        finally:
            r.eval(UNLOCK_SCRIPT, 1, lkey, node_id)
    else:
        # Wait for lock holder to populate cache
        for _ in range(6):
            await asyncio.sleep(0.05)
            jwt = r.get(ckey)
            if jwt:
                return jwt, True
        raise TimeoutError(f"CDP-2001: Cache not populated after waiting for lock on {token_id}")

async def _refresh_jwt(token_id: str, user_sub: str, scopes: list[str], r: redis_lib.Redis, node_id: str):
    """Background JWT refresh (fire-and-forget)."""
    try:
        sh = _scope_hash(scopes)
        ckey = _cache_key(token_id, sh)
        lkey = _lock_key(token_id, sh)
        if r.set(lkey, node_id, nx=True, px=5000):
            try:
                result = await keycloak_client.exchange_token(user_sub, scopes)
                r.setex(ckey, settings.jwt_cache_ttl, result["access_token"])
                r.sadd(_idx_key(token_id), ckey)
                r.expire(_idx_key(token_id), settings.jwt_ttl)
            finally:
                r.eval(UNLOCK_SCRIPT, 1, lkey, node_id)
    except Exception as e:
        logger.warning(f"Background refresh failed for {token_id}: {e}")
