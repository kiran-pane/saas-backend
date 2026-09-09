import redis.asyncio as redis_async

from app.config import settings

_redis_pool: redis_async.Redis | None = None


async def get_redis() -> redis_async.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis_async.from_url(
            settings.REDIS_URL, decode_responses=True, max_connections=50
        )
    return _redis_pool


async def close_redis() -> None:
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.close()
        _redis_pool = None
