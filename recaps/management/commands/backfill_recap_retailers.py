"""Backfill ``CustomRecap.retailer`` on specific recaps whose Retailer was
blank (so they grouped by city in the summary) or wrong.

``CustomRecap.retailer`` is the HIGHEST-priority source in
``recaps.pdf._event_retailer`` (it's checked before event.retailer /
request.retailer), so setting it makes the Girl Beer summary's
"PERFORMANCE BY RETAILER" table fold these rows into the right store on the
next sync — WITHOUT touching the shared Event (other recaps on the same event
are unaffected).

Targets are matched by (BA name + event date + store/location contains city)
using the SAME helpers the sheet export uses, so a match here is exactly the
row you see in the sheet. The default targets are the four Girl Beer rows we
identified from the [External] Girl Beer Retail Schedules sheet:

    Palm Desert      → Whole Foods   (Claire Thornhill, 06/14/2026)
    Huntington Beach → Whole Foods   (Michelle Yeh,     06/14/2026)
    Long Beach       → Whole Foods   (Giselle Espinoza, 06/21/2026)
    Fullerton 6/5    → Albertsons    (Nequisha McCarthy, 06/05/2026 — mis-tagged Vons)

Idempotent + SAFE: DRY-RUN by default. Pass --apply to write. A recap already
pointing at the target retailer is left alone ("already"). Retailers are
resolved by exact (case-insensitive) name within the tenant; a missing one is
only created on --apply.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db import transaction

# (BA full name, event date MM/DD/YYYY, city substring in Store/Location,
#  target retailer name)
DEFAULT_TARGETS = [
    {"ba": "Claire Thornhill", "date": "06/14/2026", "city": "Palm Desert", "retailer": "Whole Foods"},
    {"ba": "Michelle Yeh", "date": "06/14/2026", "city": "Huntington Beach", "retailer": "Whole Foods"},
    {"ba": "Giselle Espinoza", "date": "06/21/2026", "city": "Long Beach", "retailer": "Whole Foods"},
    {"ba": "Nequisha McCarthy", "date": "06/05/2026", "city": "Fullerton", "retailer": "Albertsons"},
]


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


class Command(BaseCommand):
    help = "Set CustomRecap.retailer on specific recaps (default: Girl Beer city→retailer fixes). Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="girl-beer")
        parser.add_argument(
            "--targets-json", default="",
            help="JSON list of {ba,date,city,retailer}; overrides the built-in defaults.",
        )
        parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")

    def handle(self, *args, **opts):
        from tenants.models import Tenant
        from events.models import Retailer
        from recaps.recap_sheet_export import (
            _ba_name, _store_location, _tenant_recaps, _fmt_mdy,
        )
        from recaps.pdf import _event_date

        slug = opts["tenant_slug"]
        apply = opts["apply"]
        targets = json.loads(opts["targets_json"]) if opts["targets_json"] else DEFAULT_TARGETS

        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            self.stdout.write(f"tenant-not-found: {slug}")
            return

        def _creator():
            # Reuse an existing tenant Retailer's creator; else a superuser;
            # else any user — only needed when we must CREATE a Retailer.
            r = Retailer.objects.filter(tenant=tenant).order_by("id").first()
            if r is not None and r.created_by_id:
                return r.created_by
            from django.contrib.auth import get_user_model
            U = get_user_model()
            return (
                U.objects.filter(is_superuser=True).order_by("id").first()
                or U.objects.order_by("id").first()
            )

        def _resolve_retailer(name: str):
            ex = (
                Retailer.objects.filter(tenant=tenant, name__iexact=name)
                .order_by("id").first()
            )
            if ex is not None:
                return ex, False
            if not apply:
                return None, True  # would create on apply
            return (
                Retailer.objects.create(tenant=tenant, name=name, created_by=_creator()),
                True,
            )

        recaps = list(_tenant_recaps(tenant))
        changed = 0
        for t in targets:
            matches = [
                r for r in recaps
                if _norm(_ba_name(r)) == _norm(t["ba"])
                and _fmt_mdy(_event_date(r)) == t["date"]
                and _norm(t["city"]) in _norm(_store_location(r))
            ]
            tag = f"{t['ba']} / {t['date']} / {t['city']} -> {t['retailer']}"
            if len(matches) != 1:
                self.stdout.write(
                    f"[{'NO-MATCH' if not matches else 'AMBIGUOUS'}] {tag} "
                    f"(matched {len(matches)})"
                )
                continue
            recap = matches[0]
            cur = getattr(recap.retailer, "name", None)
            ret, created = _resolve_retailer(t["retailer"])
            if ret is None:
                self.stdout.write(
                    f"[WOULD-CREATE-RETAILER+SET] {tag} | cur={cur!r} uuid={recap.uuid}"
                )
                continue
            if recap.retailer_id == ret.id:
                self.stdout.write(f"[already] {tag} | uuid={recap.uuid}")
                continue
            note = " (+created retailer)" if created else ""
            if apply:
                with transaction.atomic():
                    recap.retailer = ret
                    recap.save(update_fields=["retailer"])
                changed += 1
                self.stdout.write(f"[SET{note}] {tag} | was={cur!r} uuid={recap.uuid}")
            else:
                self.stdout.write(f"[WOULD-SET{note}] {tag} | cur={cur!r} uuid={recap.uuid}")

        self.stdout.write(
            f"done: tenant={slug} apply={apply} targets={len(targets)} changed={changed}"
        )
