"""LangSmith wiring. Every run is tagged with tenant_id/user_id/request_id
pulled from the request context so traces can be filtered per-tenant when
debugging a specific customer's issue, and cost/latency/error-rate can be
tracked per tenant and per model in the LangSmith dashboard."""
from langchain_core.tracers.context import tracing_v2_enabled
from langchain_core.callbacks import BaseCallbackHandler

from app.config import settings
from app.core.tenancy.context import get_tenant_optional, get_user_optional


def build_run_tags(extra: list[str] | None = None) -> list[str]:
    tags = []
    tenant_id = get_tenant_optional()
    user_id = get_user_optional()
    if tenant_id:
        tags.append(f"tenant:{tenant_id}")
    if user_id:
        tags.append(f"user:{user_id}")
    return tags + (extra or [])


def tracing_context():
    """Context manager enabling LangSmith tracing for a block of code,
    scoped to the configured project. No-op if tracing is disabled."""
    return tracing_v2_enabled(project_name=settings.LANGCHAIN_PROJECT) if settings.LANGCHAIN_TRACING_V2 \
        else _noop_context()


class _noop_context:
    def __enter__(self): return self
    def __exit__(self, *a): return False
