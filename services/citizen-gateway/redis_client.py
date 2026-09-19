import redis as redis_lib
from pathlib import Path
from config import settings

_pool = None
_quota_sha: str = None

def get_redis() -> redis_lib.Redis:
    global _pool
    if _pool is None:
        _pool = redis_lib.ConnectionPool(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password,
            decode_responses=True,
            max_connections=50,
        )
    return redis_lib.Redis(connection_pool=_pool)

def load_quota_script(r: redis_lib.Redis) -> str:
    global _quota_sha
    lua_path = Path(__file__).parent / "infra/redis/quota_deduct.lua"
    if lua_path.exists():
        _quota_sha = r.script_load(lua_path.read_text())
    return _quota_sha

def get_quota_sha() -> str:
    return _quota_sha
