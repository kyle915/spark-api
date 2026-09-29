"""Retitle already-filed recaps to "Store Name #1234" (Torch client ask).

New walk-up filings get this title from the recap form's Store boxes; this
command brings the recaps filed before that up to the same standard.

Store name: the store part of the event title (walk-in titles are
"M/D/YYYY - <address> (<store>)"), else the recap's Retailer.
Store number, first hit wins:
  1. ``--numbers-json`` — {"<store name or address>": "1234", ...} supplied
     by Ignite / the client for stores we have no number for;
  2. the event's Request.store_number;
  3. any live request of the brand at the same address with a store number.

Only system titles are rewritten (the event title, the event title + a
" · <shift>" suffix, or an earlier run's "Store Name" form). A title an admin
typed by hand is reported as ``custom`` and left alone.

SAFE: dry-run by default; ``--apply`` writes. Idempotent. The report ends
with the stores still missing a number so they can be collected.
"""
from __future__ import annotations

import json
import re
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from ambassadors.checkin_web import (
    addresses_fuzzy_match,
    compose_shift_recap_name,
    compose_store_recap_name,
    known_store_name,
    known_store_number,
    normalize_place,
    real_store_number,
    store_identity_prefill,
)

_NUMBER_SUFFIX = re.compile(r"\s#\s*[\w-]+$")


def _norm(value: str) -> str:
    return " ".join((value or "").split()).strip().lower()


def _supplied_number(numbers: dict[str, str], store_name: str, address: str) -> str:
    name_key = _norm(store_name)
    for key, number in numbers.items():
        if _norm(key) == name_key or (address and addresses_fuzzy_match(key, address)):
            return str(number).strip().lstrip("#").strip()
    return ""


def _split_shift(current: str, event_name: str, store_name: str) -> tuple[str, str]:
    """(title without " · <shift>", shift) when the suffix sits on a system title."""
    base, sep, shift = current.rpartition(" · ")
    if not sep:
        return current, ""
    unnumbered = _NUMBER_SUFFIX.sub("", base).strip()
    if base in (event_name, store_name) or unnumbered == store_name:
        return base, shift.strip()
    return current, ""


class Command(BaseCommand):
    help = 'Retitle filed recaps to "Store Name #1234". Dry-run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="keee-torch-thc")
        parser.add_argument(
            "--numbers-json",
            default="",
            help='JSON object {"<store name or address>": "<store #>"}.',
        )
        parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")

    def handle(self, *args, **opts):
        from recaps.models import CustomRecap
        from tenants.models import Tenant

        slug = opts["tenant_slug"]
        apply = opts["apply"]
        try:
            numbers = json.loads(opts["numbers_json"]) if opts["numbers_json"] else {}
        except json.JSONDecodeError as exc:
            raise CommandError(f"--numbers-json is not valid JSON: {exc}") from exc
        if not isinstance(numbers, dict):
            raise CommandError("--numbers-json must be a JSON object")

        tenant = (
            Tenant.objects.filter(slug=slug).order_by("id").first()
            or Tenant.objects.filter(request_url_name=slug).order_by("id").first()
        )
        if tenant is None:
            raise CommandError(f"tenant-not-found: {slug}")

        recaps = (
            CustomRecap.objects.filter(tenant=tenant, event__isnull=False)
            .select_related("event", "event__request", "retailer")
            .order_by("id")
        )
        counts: Counter[str] = Counter()
        missing_number: Counter[tuple[str, str]] = Counter()
        address_numbers: dict[str, str] = {}
        address_names: dict[str, str] = {}

        for recap in recaps.iterator():
            event = recap.event
            current = (recap.name or "").strip()
            event_name = (event.name or "").strip()
            address = (event.address or "").strip()
            store_name = store_identity_prefill(event)["name"] or (
                (recap.retailer.name or "").strip() if recap.retailer_id else ""
            )
            if not store_name and address:
                addr_key = normalize_place(address)
                if addr_key not in address_names:
                    address_names[addr_key] = known_store_name(tenant.id, address)
                store_name = address_names[addr_key]
            if not store_name:
                counts["no-store-name"] += 1
                self.stdout.write(f"no-store-name  #{recap.id}  {current!r}")
                continue

            base, shift = _split_shift(current, event_name, store_name)
            unnumbered = _NUMBER_SUFFIX.sub("", base).strip()
            if base != event_name and unnumbered != store_name:
                counts["custom"] += 1
                self.stdout.write(f"custom         #{recap.id}  {current!r}")
                continue

            number = _supplied_number(numbers, store_name, address)
            if not number:
                req = getattr(event, "request", None)
                number = real_store_number(req.store_number) if req is not None else ""
            if not number and address:
                addr_key = normalize_place(address)
                if addr_key not in address_numbers:
                    address_numbers[addr_key] = known_store_number(tenant.id, address)
                number = address_numbers[addr_key]
            if not number and base != event_name and base.startswith(store_name):
                number = real_store_number(base[len(store_name):])

            proposed = compose_store_recap_name(store_name, number)
            if shift:
                proposed = compose_shift_recap_name(proposed, shift)
            if not number:
                missing_number[(store_name, address)] += 1

            if proposed == current:
                counts["already"] += 1
                continue
            counts["renamed" if number else "renamed-no-number"] += 1
            self.stdout.write(f"rename         #{recap.id}  {current!r} -> {proposed!r}")
            if apply:
                CustomRecap.objects.filter(id=recap.id).update(name=proposed[:255])

        mode = "APPLIED" if apply else "DRY-RUN"
        self.stdout.write(f"\n{mode} {slug}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if missing_number:
            self.stdout.write(f"\nStores with no store # on file ({len(missing_number)}):")
            for (store, address), n in sorted(missing_number.items()):
                self.stdout.write(f"  {store} | {address} | {n} recap(s)")
