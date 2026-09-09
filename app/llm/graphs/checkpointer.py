"""LangGraph checkpoints live in the same Postgres instance as the rest of
the app data (separate tables managed by langgraph-checkpoint-postgres) —
one less stateful system to operate, and it inherits the same
backup/HA story as everything else.

IMPORTANT: AsyncPostgresSaver.from_conn_string(...) returns an async
context manager wrapping a connection pool, not a ready-to-use instance —
calling .setup() on the context manager object itself is a no-op/error.
We enter it once at app startup (see app/main.py's lifespan) and hold it
open for the process lifetime via an AsyncExitStack, closing it cleanly
on shutdown.
"""
from contextlib import AsyncExitStack

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import settings

_checkpointer: AsyncPostgresSaver | None = None
_exit_stack: AsyncExitStack | None = None


async def init_checkpointer() -> AsyncPostgresSaver:
    """Call once during app startup (FastAPI lifespan) and once per
    Celery worker process before any graph invocation."""
    global _checkpointer, _exit_stack
    if _checkpointer is not None:
        return _checkpointer

    conn_str = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    _exit_stack = AsyncExitStack()
    _checkpointer = await _exit_stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(conn_str)
    )
    await _checkpointer.setup()  # creates checkpoint tables if not already present
    return _checkpointer


async def get_checkpointer() -> AsyncPostgresSaver:
    if _checkpointer is None:
        return await init_checkpointer()
    return _checkpointer


async def close_checkpointer() -> None:
    global _checkpointer, _exit_stack
    if _exit_stack is not None:
        await _exit_stack.aclose()
    _checkpointer = None
    _exit_stack = None
