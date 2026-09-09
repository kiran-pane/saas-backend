from prometheus_fastapi_instrumentator import Instrumentator


def configure_metrics(app) -> None:
    """Exposes /metrics with request latency/count/error-rate histograms
    out of the box. Add custom counters (LLM tokens, cache hit ratio,
    Celery queue depth) alongside this via prometheus_client directly in
    the modules that own that data (e.g. app/llm/providers/registry.py)."""
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
