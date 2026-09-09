import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.tenancy.context import get_tenant_optional

log = structlog.get_logger("access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds request_id/tenant_id/user_id into structlog context for the
    lifetime of the request, so every log line (and, via app/llm/tracing.py,
    every LangSmith run) emitted while handling it can be correlated by a
    single request_id — the backbone of debugging a single user's report."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        tenant_id = get_tenant_optional()
        log.info(
            "request_complete",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
        response.headers["X-Request-ID"] = request_id
        return response
