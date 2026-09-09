import time

from fastapi import Depends, Request

from app.core.cache.redis_client import get_redis
from app.core.exceptions import RateLimitError
from app.core.tenancy.context import get_tenant

_SLIDING_WINDOW_LUA = """
local key, now, window, limit = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count < limit then
    redis.call('ZADD', key, now, now .. '-' .. math.random())
    redis.call('EXPIRE', key, window)
    return 1
end
return 0
"""


async def _check(scope: str, limit: int, window_seconds: int, identity: str) -> None:
    redis = await get_redis()
    key = f"rl:{get_tenant()}:{scope}:{identity}"
    allowed = await redis.eval(
        _SLIDING_WINDOW_LUA, 1, key, str(time.time()), str(window_seconds), str(limit)
    )
    if not allowed:
        raise RateLimitError(retry_after=window_seconds)


async def check_custom_rate_limit(scope: str, limit: int, window_seconds: int, identity: str) -> None:
    """Public entry point for rate-limiting on a dimension that isn't
    "current user" or "caller IP" — e.g. per-email brute-force limiting on
    login, keyed on the request body's email field before any DB lookup
    happens. Call directly from route handlers rather than as a FastAPI
    dependency when the identity comes from the parsed request body."""
    await _check(scope, limit, window_seconds, identity)


def _lazy_get_current_user():
    # Imported lazily (function-call time, not module-import time) to
    # avoid a circular import: app.api.deps imports from app.core.*, so
    # importing app.api.deps at module scope here would create a cycle.
    from app.api.deps import get_current_user

    return get_current_user


def rate_limit(scope: str, limit: int, window_seconds: int, by: str = "user"):
    """FastAPI dependency factory. Scoped per-tenant AND per-identity so one
    noisy tenant/user can't starve others sharing the same infrastructure.

    by="user"  -> keys on the authenticated user. The identity is resolved
                  via a real FastAPI sub-dependency (Depends(get_current_user)),
                  not a request.state read — this matters because FastAPI
                  does not guarantee a route's `dependencies=[...]` list
                  runs after its parameter dependencies, so reading
                  request.state.current_user here was a real ordering bug.
                  Making it an explicit sub-dependency also means FastAPI's
                  default per-request caching kicks in: get_current_user
                  is resolved once and shared with the route's own
                  `user: User = Depends(get_current_user)` parameter.
    by="ip"    -> keys on the caller's IP. Use for pre-auth routes like
                  login/signup where there's no authenticated user yet.
    """

    if by == "user":
        async def dependency(request: Request, _user=Depends(_lazy_get_current_user())):
            await _check(scope, limit, window_seconds, str(_user.id))

        return dependency

    if by == "ip":
        async def dependency(request: Request):
            identity = request.client.host if request.client else "unknown"
            await _check(scope, limit, window_seconds, identity)

        return dependency

    raise ValueError(f"Unknown rate_limit 'by' value: {by!r}. Use 'user' or 'ip'.")
