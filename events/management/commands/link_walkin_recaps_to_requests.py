"""Attach filed walk-up Events onto Tracker Requests that still show DUE.

Famus (and similar walk-up brands) often file a CustomRecap on a
request-less walk-in Event while the Master Tracker row keeps its own
scheduled Event with zero filed content — Tracker reads ``DUE``, Missing
Recaps nags ``No BA assigned``.

This command finds high-confidence pairs and sets ``orphan.request_id``
so ``recapsFiledCount`` clears. Ambiguous multiples (two orphans, two
requests, or chain banners repeating the same day) are reported and
skipped.

Match rules (all required):
  * same tenant
  * same calendar day
  * fuzzy address match OR unique store-name match that day
  * orphan has at least one *filed* recap and ``request_id`` is null
  * target Request's events currently have zero filed recaps
  * exactly one orphan and exactly one DUE request for that place/day

Dry-run is the default. Pass ``--apply`` to write.

Usage:
    python manage.py link_walkin_recaps_to_requests --tenant-slug famus-energy
    python manage.py link_walkin_recaps_to_requests --tenant-slug famus-energy --apply
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as djtz

from ambassadors.checkin_web import (
    addresses_fuzzy_match,
    event_calendar_date,
    normalize_place,
)
from events.models import Event, Request
from recaps.filed import custom_filed_q, legacy_filed_q
from recaps.models import CustomRecap, Recap
from tenants.models import Tenant


def _name_key(value: str) -> str:
    """Strip date/bullet suffixes so 'Smiley's · 2026-10-22' → 'smileys'."""
    raw = value or ""
    raw = re.sub(r"[•·].*$", "", raw)
    raw = re.sub(r"\d{1,2}/\d{1,2}/\d{2,4}", "", raw)
    raw = re.sub(r"\d{4}-\d{2}-\d{2}", "", raw)
    return normalize_place(raw)


def _labels(ev: Event) -> list[str]:
    out = [getattr(ev, "name", "") or ""]
    retailer = getattr(ev, "retailer", None)
    if retailer is not None:
        out.append(getattr(retailer, "name", "") or "")
    return [x for x in out if x]


def _same_place(a: Event, b: Event) -> bool:
    if addresses_fuzzy_match(a.address or "", b.address or ""):
        return True
    a_keys = {_name_key(x) for x in _labels(a) if _name_key(x)}
    b_keys = {_name_key(x) for x in _labels(b) if _name_key(x)}
    # Require a meaningful token (≥4 chars) so "Shell"/"BP" alone never
    # decide — uniqueness on the day still gates the final pair.
    shared = {k for k in a_keys & b_keys if len(k) >= 4}
    return bool(shared)


def _has_filed(event_id: int) -> bool:
    return (
        CustomRecap.objects.filter(event_id=event_id)
        .filter(custom_filed_q())
        .exists()
        or Recap.objects.filter(event_id=event_id).filter(legacy_filed_q()).exists()
    )


class Command(BaseCommand):
    help = (
        "Link filed request-less walk-up Events onto Tracker Requests that "
        "still show DUE. High-confidence only. Dry-run default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-slug",
            type=str,
            default="famus-energy",
            help="Exact tenant slug (default: famus-energy).",
        )
        parser.add_argument("--days-back", type=int, default=45)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write request_id on matched orphans.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        slug = (opts["tenant_slug"] or "").strip()
        if not slug:
            raise CommandError("--tenant-slug is required.")
        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            tenant = Tenant.objects.filter(request_url_name=slug).first()
        if tenant is None:
            raise CommandError(f"No tenant with slug/request_url_name={slug!r}.")

        if not apply:
            self.stdout.write(
                self.style.WARNING("DRY RUN — pass --apply to write links.\n")
            )

        now = djtz.now()
        lo = now - timedelta(days=max(1, int(opts["days_back"])))

        orphans = list(
            Event.objects.filter(
                tenant=tenant,
                request__isnull=True,
                date__gte=lo,
                co_activation_host__isnull=True,
            )
            .select_related("retailer")
            .order_by("id")
        )
        orphans = [ev for ev in orphans if _has_filed(ev.id)]

        due_requests = list(
            Request.objects.filter(
                tenant=tenant,
                deleted_at__isnull=True,
                date__gte=lo,
            )
            .prefetch_related("event_set__retailer")
            .order_by("id")
        )

        linked = skipped = ambiguous = 0
        report: list[dict] = []

        claimed_orphan: set[int] = set()
        claimed_request: set[int] = set()

        for req in due_requests:
            events = list(req.event_set.all())
            if not events:
                continue
            if any(_has_filed(ev.id) for ev in events):
                continue  # already shows RECAP on Tracker
            day = None
            for ev in events:
                day = event_calendar_date(ev) or day
            if day is None and getattr(req, "date", None) is not None:
                rd = req.date
                day = rd.date() if hasattr(rd, "date") else rd

            candidates = []
            for orphan in orphans:
                if orphan.id in claimed_orphan:
                    continue
                oday = event_calendar_date(orphan)
                if day is not None and oday is not None and oday != day:
                    continue
                if any(_same_place(orphan, shell) for shell in events):
                    candidates.append(orphan)

            if not candidates:
                continue
            if len(candidates) != 1:
                ambiguous += 1
                report.append(
                    {
                        "status": "ambiguous-orphans",
                        "request_id": req.id,
                        "request_uuid": str(req.uuid),
                        "orphan_ids": [c.id for c in candidates],
                    }
                )
                continue

            # Also refuse if another DUE request on the same day would claim
            # the same orphan (two Smileys same day = skip).
            orphan = candidates[0]
            rival_requests = [
                r
                for r in due_requests
                if r.id != req.id
                and r.id not in claimed_request
                and not any(_has_filed(ev.id) for ev in r.event_set.all())
                and any(
                    _same_place(orphan, shell)
                    and (
                        day is None
                        or event_calendar_date(shell) in (None, day)
                    )
                    for shell in r.event_set.all()
                )
            ]
            if rival_requests:
                ambiguous += 1
                report.append(
                    {
                        "status": "ambiguous-requests",
                        "orphan_id": orphan.id,
                        "request_ids": [req.id] + [r.id for r in rival_requests],
                    }
                )
                continue

            claimed_orphan.add(orphan.id)
            claimed_request.add(req.id)
            entry = {
                "status": "link" if apply else "would-link",
                "request_id": req.id,
                "request_uuid": str(req.uuid),
                "request_name": req.name,
                "orphan_id": orphan.id,
                "orphan_uuid": str(orphan.uuid),
                "orphan_name": orphan.name,
                "day": day.isoformat() if day else None,
            }
            report.append(entry)
            self.stdout.write(
                f"  {'+' if apply else '~'} REQ-{req.id} ← event {orphan.id} "
                f"({orphan.name!r} → {req.name!r})"
            )
            if apply:
                with transaction.atomic():
                    Event.objects.filter(pk=orphan.id, request__isnull=True).update(
                        request_id=req.id
                    )
                linked += 1
            else:
                skipped += 1

        self.stdout.write(
            f"\ntenant={tenant.slug} linked={linked} would_link={skipped} "
            f"ambiguous={ambiguous} orphans_with_filed={len(orphans)}"
        )
        self.stdout.write(
            "JSON_RESULT:"
            + json.dumps(
                {
                    "tenant": tenant.slug,
                    "apply": apply,
                    "linked": linked,
                    "would_link": skipped,
                    "ambiguous": ambiguous,
                    "rows": report,
                }
            )
        )
