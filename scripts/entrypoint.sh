#!/usr/bin/env bash
# Cloud Run / container entrypoint.
#
# Runs Django migrations before starting the ASGI server so each
# deploy of a new image lands its migrations the moment the first
# container boots. Migrations are idempotent and protected by an
# advisory lock on django_migrations, so parallel cold starts are
# safe — first container wins, the rest no-op.
#
# Behavior is gated on RUN_MIGRATIONS_ON_BOOT (default 1). Set to
# 0 if you ever want to roll back to "manage migrations out of
# band" — e.g. for a destructive rename you'd rather run by hand.

set -euo pipefail

RUN_MIGRATIONS_ON_BOOT="${RUN_MIGRATIONS_ON_BOOT:-1}"

if [ "$RUN_MIGRATIONS_ON_BOOT" = "1" ]; then
  echo ">>> entrypoint: running migrations"
  uv run python manage.py migrate --noinput
  echo ">>> entrypoint: migrations complete"
else
  echo ">>> entrypoint: RUN_MIGRATIONS_ON_BOOT=0, skipping migrate"
fi

echo ">>> entrypoint: starting hypercorn"
exec uv run hypercorn config.asgi:application --bind "0.0.0.0:${PORT:-8000}"
