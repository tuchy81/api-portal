import asyncpg
from config import settings

_pool: asyncpg.Pool = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.db_dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
    return _pool

async def get_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
