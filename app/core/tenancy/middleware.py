from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.exceptions import NotFoundError
from app.core.tenancy.context import set_tenant
from app.core.tenancy.resolver import resolve_tenant

# Routes that legitimately have no tenant context (health checks, platform
# admin bootstrap, tenant signup itself).
EXEMPT_PATH_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json", "/metrics", "/api/v1/platform",
                         "/api/v1/dev-storage", "/api/v1/webhooks", "/api/v1/media/live-hls")


class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(EXEMPT_PATH_PREFIXES):
            return await call_next(request)

        tenant = await resolve_tenant(request)
        if tenant is None:
            raise NotFoundError("Unknown tenant")
        if tenant.status != "active":
            raise NotFoundError("Tenant is not active")

        set_tenant(tenant.id)
        return await call_next(request)
