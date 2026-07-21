"""Dump a tenant's field-sampling results as JSON — read-only reporting.

Built to refresh the Feel Free routes site (kyle915.github.io/feel-free-routes)
without a local DB proxy: per-market YTD SKU breakdowns + field call-outs +
weekly sample buckets, everything the site's RECAP dict and Sampling Recap tab
need, in one JSON blob. Pure reads — no writes, no side effects.

Run via the ``/internal/cron/dump-field-sampling`` endpoint (or the
"Dump field sampling" GitHub Action); the JSON lands in the workflow run log.
"""

from __future__ import annotations

import datetime
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

DEFAULT_MARKETS = [
    "Miami",
    "Ft. Lauderdale",
    "Tampa / St. Pete",
    "Austin",
    "San Antonio",
]


class Command(BaseCommand):
    help = "Dump per-market field-sampling results (SKU totals, call-outs, weekly buckets) as JSON."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="feel-free",
            help="tenant slug or numeric id (default: feel-free)",
        )
        parser.add_argument(
            "--markets",
            default=",".join(DEFAULT_MARKETS),
            help="comma-separated market labels (default: the five Feel Free metros)",
        )
        parser.add_argument(
            "--anchor",
            default="2026-06-25",
            help="YYYY-MM-DD Thursday anchoring the 7-day weekly buckets (default 2026-06-25)",
        )

    def handle(self, *args, **opts):
        from tenants.models import Tenant
        from recaps.field_sampling_report import (
            _ytd_window,
            field_callouts,
            sku_breakdown,
        )

        ident = str(opts["tenant"]).strip()
        tenant = (
            Tenant.objects.filter(id=int(ident)).first()
            if ident.isdigit()
            else Tenant.objects.filter(slug=ident).first()
        )
        if tenant is None:
            names = ", ".join(
                f"{t.id}:{t.slug}" for t in Tenant.objects.order_by("id")
            )
            raise CommandError(f"No tenant matches {ident!r}. Known: {names}")

        markets = [m.strip() for m in str(opts["markets"]).split(",") if m.strip()]
        try:
            anchor_date = datetime.date.fromisoformat(str(opts["anchor"]))
        except ValueError as exc:
            raise CommandError(f"Bad --anchor {opts['anchor']!r}: {exc}")

        ytd_start, ytd_end = _ytd_window()
        now = timezone.now()
        out: dict = {
            "tenant": {"id": tenant.id, "slug": tenant.slug, "name": tenant.name},
            "ytd_window": [ytd_start.isoformat(), ytd_end.isoformat()],
            "now": now.isoformat(),
            # Unfiltered program total — if it exceeds the sum of the listed
            # markets, a new market label (or unparseable event names) exists.
            "overall": sku_breakdown(tenant.id, ytd_start, ytd_end),
            "markets": {},
            "weekly": [],
        }

        for m in markets:
            out["markets"][m] = {
                "sku": sku_breakdown(tenant.id, ytd_start, ytd_end, market=m),
                "callouts": field_callouts(tenant.id, ytd_start, ytd_end, market=m),
            }

        # Weekly buckets: 7-day windows from the anchor (the site's Thursday
        # WEEK_ANCHOR) through now → the Sampling Recap market × week grid.
        anchor = timezone.make_aware(
            datetime.datetime.combine(anchor_date, datetime.time.min)
        )
        k = 0
        while True:
            ws = anchor + datetime.timedelta(days=7 * k)
            if ws > now:
                break
            we = ws + datetime.timedelta(days=7)
            wk = {
                "start": ws.date().isoformat(),
                "end": (we - datetime.timedelta(days=1)).date().isoformat(),
                "markets": {},
            }
            for m in markets:
                b = sku_breakdown(tenant.id, ws, we, market=m)
                wk["markets"][m] = {
                    "mode": b.get("mode"),
                    "total": sum(int(i.get("total") or 0) for i in b.get("items", [])),
                    "items": b.get("items", []),
                }
            out["weekly"].append(wk)
            k += 1

        self.stdout.write("FFDUMP_JSON_START")
        self.stdout.write(json.dumps(out, default=str))
        self.stdout.write("FFDUMP_JSON_END")
