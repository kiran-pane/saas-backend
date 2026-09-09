"""Model provider registry: config-driven selection + fallback chains.

Swapping the default model, adding a provider, or giving a specific tenant
a dedicated model is a config change here, not a code change anywhere else
in the app. Services should always call get_model() — never instantiate
ChatOpenAI/ChatAnthropic directly.
"""
from collections.abc import Callable

from langchain_core.runnables import Runnable

from app.config import settings

_FACTORIES: dict[str, Callable[[], Runnable]] = {}


def _lazy_openai(model: str):
    def factory():
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=settings.OPENAI_API_KEY, timeout=30, max_retries=2)
    return factory


def _lazy_anthropic(model: str):
    def factory():
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=settings.ANTHROPIC_API_KEY, timeout=30, max_retries=2)
    return factory


_FACTORIES["openai:gpt-4.1"] = _lazy_openai("gpt-4.1")
_FACTORIES["openai:gpt-4.1-mini"] = _lazy_openai("gpt-4.1-mini")
_FACTORIES["anthropic:claude-sonnet"] = _lazy_anthropic("claude-sonnet-4-6")
_FACTORIES["anthropic:claude-haiku"] = _lazy_anthropic("claude-haiku-4-5")


class UnknownModelError(Exception):
    pass


def get_model(primary: str | None = None, fallbacks: list[str] | None = None) -> Runnable:
    primary = primary or settings.DEFAULT_LLM_MODEL
    fallbacks = fallbacks if fallbacks is not None else settings.DEFAULT_LLM_FALLBACKS

    if primary not in _FACTORIES:
        raise UnknownModelError(f"Unknown model: {primary}")

    model = _FACTORIES[primary]().with_retry(stop_after_attempt=2)

    if fallbacks:
        fallback_models = [_FACTORIES[f]() for f in fallbacks if f in _FACTORIES]
        if fallback_models:
            return model.with_fallbacks(fallback_models)
    return model


def register_provider(name: str, factory: Callable[[], Runnable]) -> None:
    """Extension point for adding a new provider (e.g. a local/self-hosted
    model) without touching this module's internals elsewhere."""
    _FACTORIES[name] = factory
