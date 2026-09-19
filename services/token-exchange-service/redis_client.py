import redis as redis_lib
from config import settings
from pathlib import Path

_pool = None
_quota_sha: str = None
_unlock_sha: str = None

def get_redis() -> redis_lib.Redis:
    global _pool
    if _pool is None:
        _pool = redis_lib.ConnectionPool(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            decode_responses=True,
            max_connections=20,
        )
    return redis_lib.Redis(connection_pool=_pool)

def load_lua_scripts(r: redis_lib.Redis):
    global _unlock_sha
    unlock_lua = Path(__file__).parent / "infra/redis/unlock_if_mine.lua"
    if unlock_lua.exists():
        _unlock_sha = r.script_load(unlock_lua.read_text())

def get_unlock_sha() -> str:
    return _unlock_sha
