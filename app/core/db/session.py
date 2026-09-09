from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# NullPool-equivalent behavior: since PgBouncer (transaction mode) already
# pools connections between the app and Postgres, the app-side engine keeps
# a small pool and leans on PgBouncer for the heavy lifting. Tune
# DB_POOL_SIZE per replica so (replicas * pool_size) stays under PgBouncer's
# max_client_conn.
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=(settings.ENV == "development"),
)

# NOTE on pgvector + asyncpg: rather than registering pgvector's asyncpg
# codec via a connection-pool event hook (awkward to get right — it
# requires awaiting async setup work inside a synchronous pool-connect
# callback, with no clean guarantee the registration completes before
# the very first query on a freshly opened connection), this codebase
# handles `vector` column (de)serialization EXPLICITLY at every call
# site instead: embeddings are passed as pgvector literal strings
# ('[0.1,0.2,...]') cast with `::vector` in the SQL, and read back via
# `::text` cast + a small parser. See
# app/llm/ingestion/pipeline.py::_vector_to_literal / _parse_vector_literal.
# More verbose per call site, but fully deterministic — no reliance on
# connection-lifecycle timing.

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# ---- System/operator session (deliberately bypasses per-request RLS scoping) ----
# Used ONLY by genuinely cross-tenant background workloads that must see
# every tenant's rows at once — currently: the live-stream supervisor
# (app/streaming/live_manager.py), which polls live_stream_sources across
# ALL tenants continuously, not one tenant per invocation. This is
# fundamentally different from a request-scoped or single-tenant Celery
# task (which always sets `app.current_tenant` via get_db()/SET LOCAL)
# and different from the payment-webhook case (which resolves ONE
# tenant_id via a small non-RLS index table, then proceeds normally).
#
# SYSTEM_DATABASE_URL defaults to the same value as DATABASE_URL, which
# is fine in early-stage / owner-role setups where RLS is bypassed by
# table ownership anyway (see migration 0004's note). In a hardened
# production deployment where the app runs under a non-owning,
# `FORCE ROW LEVEL SECURITY`-subject role, SYSTEM_DATABASE_URL MUST point
# to a separate role with `BYPASSRLS` — never the same restricted role —
# and that role's credentials must be tightly held (this is a genuine
# privilege escalation path if leaked, unlike the normal app role).
system_engine = create_async_engine(
    settings.SYSTEM_DATABASE_URL or settings.DATABASE_URL,
    pool_size=2, max_overflow=2, pool_pre_ping=True, pool_recycle=1800,
    echo=(settings.ENV == "development"),
)

system_session_factory = async_sessionmaker(
    bind=system_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)
