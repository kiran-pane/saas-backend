from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging.middleware import RequestContextMiddleware
from app.core.logging.setup import configure_logging
from app.core.observability.metrics import configure_metrics
from app.core.observability.tracing import configure_tracing
from app.core.tenancy.middleware import TenantMiddleware


def _validate_cors_config() -> None:
    """Cheap insurance against a config mistake that browsers mostly
    reject anyway, but a proxy/CDN in front of the API could still
    normalize headers in a way that reintroduces risk — fail loudly at
    startup rather than silently shipping a wildcard+credentials
    combination to production."""
    if "*" in settings.CORS_ORIGINS and settings.CORS_ALLOW_CREDENTIALS:
        raise RuntimeError(
            "CORS_ORIGINS contains '*' while credentials are allowed — this is a "
            "serious misconfiguration (any origin could read authenticated "
            "responses). Set explicit origins in CORS_ORIGINS."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    from app.llm.graphs.checkpointer import close_checkpointer, init_checkpointer

    await init_checkpointer()
    yield
    from app.core.cache.redis_client import close_redis

    await close_redis()
    await close_checkpointer()


def create_app() -> FastAPI:
    _validate_cors_config()

    app = FastAPI(
        title="SaaS Backend",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.ENV != "production" else None,
        redoc_url="/redoc" if settings.ENV != "production" else None,
    )

    # Order matters: Starlette applies the FIRST-added middleware as the
    # OUTERMOST layer. We want, outer to inner:
    #   CORS -> RequestContext -> Tenant -> routes
    # CORS outermost so preflight OPTIONS never touches tenant resolution.
    # RequestContext before Tenant so request_id is already bound by the
    # time TenantMiddleware runs — otherwise an "unknown tenant" 404
    # raised by TenantMiddleware would be logged/returned without a
    # request_id, breaking correlation for exactly the requests where
    # you're most likely to be debugging a client misconfiguration.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Tenant-Slug", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(TenantMiddleware)

    register_exception_handlers(app)
    configure_metrics(app)
    configure_tracing(app)

    app.include_router(api_router)
    return app


app = create_app()
