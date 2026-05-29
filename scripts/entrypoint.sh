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

# One-time, idempotent repair of the Girl Beer recap template so it matches the
# Connecteam export (adds the missing fields + renames the drifted labels).
# Safe to re-run — a clean template is a no-op — and a no-op in envs without the
# tenant (|| true swallows "no tenant"). A pg advisory lock inside the command
# serializes parallel cold starts. Set RUN_GIRL_BEER_REPAIR_ON_BOOT=0 to disable
# once it's confirmed applied in prod.
if [ "${RUN_GIRL_BEER_REPAIR_ON_BOOT:-1}" = "1" ]; then
  echo ">>> entrypoint: repairing Girl Beer recap template (idempotent)"
  uv run python manage.py repair_girl_beer_template --tenant-slug girl-beer || true
  echo ">>> entrypoint: Girl Beer template repair done"
fi

# Backfill JPG siblings for existing HEIC recap files so the recap views render a
# real photo instead of the in-browser-converter fallback tile. New uploads convert
# at upload time; this catches files that predate that. Idempotent (skips files that
# already have a .jpg sibling) and run in the BACKGROUND so it never delays boot or
# risks the startup health check — hypercorn starts immediately and files "light up"
# as each sibling lands. Set RUN_HEIC_BACKFILL_ON_BOOT=0 to disable once confirmed.
if [ "${RUN_HEIC_BACKFILL_ON_BOOT:-1}" = "1" ]; then
  echo ">>> entrypoint: backfilling HEIC->JPG siblings in background (idempotent)"
  ( uv run python manage.py backfill_heic_jpg_siblings --apply || true ) &
fi

echo ">>> entrypoint: starting hypercorn"
exec uv run hypercorn config.asgi:application --bind "0.0.0.0:${PORT:-8000}"
