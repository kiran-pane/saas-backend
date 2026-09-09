from collections.abc import Callable
from functools import wraps

import orjson

from app.core.cache.redis_client import get_redis


def cached(ttl: int, key_builder: Callable[..., str]):
    """Read-through cache decorator for async functions.

    key_builder receives the same args/kwargs as the wrapped function and
    must return a fully-qualified cache key (include tenant_id — never
    build a key that could collide across tenants).
    """

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            redis = await get_redis()
            key = key_builder(*args, **kwargs)
            hit = await redis.get(key)
            if hit is not None:
                return orjson.loads(hit)
            result = await fn(*args, **kwargs)
            await redis.setex(key, ttl, orjson.dumps(result, default=str))
            return result

        return wrapper

    return decorator


async def invalidate(key: str) -> None:
    redis = await get_redis()
    await redis.delete(key)
