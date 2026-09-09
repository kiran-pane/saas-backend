"""Seed CLI entrypoint.

Usage:
    python -m app.seeds.run permissions   # sync the permission catalog (idempotent)
    python -m app.seeds.run dev-data      # create SEED_TENANT_SLUG + owner user, if configured (idempotent)
    python -m app.seeds.run all           # both, in the right order

Via Docker:
    docker compose exec api python -m app.seeds.run all
"""
import argparse
import asyncio

import structlog

from app.core.db.session import async_session_factory
from app.core.logging.setup import configure_logging
from app.seeds.dev_data import seed_dev_environment
from app.seeds.permissions import sync_permission_catalog

log = structlog.get_logger("seeds")


async def run(command: str) -> None:
    async with async_session_factory() as db:
        if command in ("permissions", "all"):
            count = await sync_permission_catalog(db)
            await db.commit()
            log.info("permissions_synced", count=count)

        if command in ("dev-data", "all"):
            result = await seed_dev_environment(db)
            await db.commit()
            log.info("dev_data_seeded", result=result or "nothing to do (SEED_TENANT_SLUG unset)")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Seed baseline/dev data for the SaaS backend")
    parser.add_argument("command", choices=["permissions", "dev-data", "all"])
    args = parser.parse_args()
    asyncio.run(run(args.command))


if __name__ == "__main__":
    main()
