#!/usr/bin/env sh
set -e

# Run pending migrations before the app starts serving traffic. Safe to
# run on every container start — Alembic no-ops if already at head. This
# is what makes `docker compose up` produce a fully-migrated, ready-to-use
# project on first run without a separate manual step.
echo "Running database migrations..."
alembic upgrade head

# Opt-in: idempotent permission-catalog sync (upsert, never touches
# existing role grants — see app/seeds/permissions.py). Off by default;
# enable via SEED_PERMISSIONS_ON_STARTUP=true in .env, or run manually:
#   docker compose exec api python -m app.seeds.run permissions
if [ "${SEED_PERMISSIONS_ON_STARTUP:-false}" = "true" ]; then
    echo "Syncing permission catalog..."
    python -m app.seeds.run permissions
fi

echo "Starting application..."
exec "$@"
