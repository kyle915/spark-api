#!/usr/bin/env bash
# Cloud Run / container entrypoint.
#
# Migrations and the Girl Beer recap-template repair run on deploy
# (Cloud Run Job `spark-api-migrate`, see deploy-cloud-run.yml) — not
# on every container boot. Scale-from-zero / min-instance replacement
# used to block on `migrate` + `repair_girl_beer_template` before
# hypercorn bound the port; that was the morning first-click stall.
#
# Opt back into boot-time migrate with RUN_MIGRATIONS_ON_BOOT=1
# (destructive rename you'd rather run by hand: leave at 0).
#
# `migrate-only` is the deploy job command: apply schema + Girl Beer
# repair, then exit. It is not a request-serving path.

set -euo pipefail

if [ "${1:-}" = "migrate-only" ]; then
  echo ">>> entrypoint: migrate-only (deploy job)"
  uv run python manage.py migrate --noinput
  echo ">>> entrypoint: migrations complete"
  echo ">>> entrypoint: repairing Girl Beer recap template (idempotent)"
  uv run python manage.py repair_girl_beer_template --tenant-slug girl-beer || true
  echo ">>> entrypoint: Girl Beer template repair done"
  exit 0
fi

RUN_MIGRATIONS_ON_BOOT="${RUN_MIGRATIONS_ON_BOOT:-0}"

if [ "$RUN_MIGRATIONS_ON_BOOT" = "1" ]; then
  echo ">>> entrypoint: running migrations"
  uv run python manage.py migrate --noinput
  echo ">>> entrypoint: migrations complete"
else
  echo ">>> entrypoint: RUN_MIGRATIONS_ON_BOOT=0, skipping migrate"
fi

# One-time, idempotent repair of the Girl Beer recap template so it matches the
# Connecteam export (adds the missing fields + renames the drifted labels).
# Off by default — the deploy job runs it. Set RUN_GIRL_BEER_REPAIR_ON_BOOT=1
# only if you need a one-off boot-time catch-up.
if [ "${RUN_GIRL_BEER_REPAIR_ON_BOOT:-0}" = "1" ]; then
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
