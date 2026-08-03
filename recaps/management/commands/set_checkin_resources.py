"""Set the BA-facing resource buttons on a tenant's check-in page.

The check-in page used to surface exactly ONE reference link per brand
(``Tenant.checkin_training_url`` — Liquid Death's ``/training/<code>`` hub).
Feel Free needs two things that are not the same kind of thing:

1. a **BA training guide** (the Summer Street Sampling deck) the BA reads, and
2. a **photo release QR** the BA *displays* so a consumer can scan it off their
   screen and sign their own release.

(2) is why this is a list with a ``kind`` rather than a second URL column. A QR
behind a hyperlink is useless — you cannot scan a code with the phone that is
showing it — so the page has to know to render that entry as a big inline image
instead of a link. See ``tenants.models.CHECKIN_RESOURCE_KINDS``.

Dry-run by default; ``--apply`` writes. Idempotent: the desired list is
normalised and compared against what is already stored, so a re-run with no
content change writes nothing and says so.

Run via ``/internal/cron/set-checkin-resources`` (or the "Set check-in
resources" GitHub Action) so it executes against prod.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from tenants.models import (
    MAX_CHECKIN_RESOURCES,
    Tenant,
    normalize_checkin_resources,
)

# Where the assets are served from. They live in the FRONT-END repo under
# `public/training/<brand>/` and ride its Firebase deploy — same call as the LD
# field guide and PDFs. That buys no GCS credentials, no CORS preflight, and
# versioning with the app; `firebase.json` already grants `/training/**` a
# 7-day Cache-Control (matched last, so it beats the blanket no-store rule).
ASSET_BASE = "https://admin.igniteproductions.co"

# Per-brand defaults, keyed by the same loose tenant needle the other setup
# commands take. Keeping them here rather than in the workflow inputs means the
# QR/deck URLs are reviewable in git instead of living only in a form field
# somebody has to retype correctly at 6am.
PRESETS: dict[str, list[dict]] = {
    "feel free": [
        {
            "label": "BA Training Guide",
            "kind": "pdf",
            "url": f"{ASSET_BASE}/training/ff/ba-training-guide.pdf",
            "note": "Summer street sampling deck · 23 slides",
        },
        {
            "label": "Photo Release Form",
            "kind": "image",
            # Encodes https://app.waiverforever.com/pending/3dXd8QiEzV1785507605
            # — Botanic Tonics' "Consent & Release Agreement for Use of
            # Product". Regenerated at 1230px from that URL rather than shipping
            # the 300px screenshot, so it stays sharp when the page blows it up
            # full-width; a blurry QR is a QR that doesn't scan.
            "url": f"{ASSET_BASE}/training/ff/photo-release-qr.png",
            "note": "Show this — the consumer scans it to sign",
        },
    ],
}


class Command(BaseCommand):
    help = (
        "Set the BA resource buttons (training guide, photo-release QR, "
        "reference hub) on a tenant's check-in page (dry-run default)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="feel free")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write. Without this the command only reports.",
        )
        parser.add_argument(
            "--resources",
            default="",
            help=(
                "JSON array of {label, kind, url, note} to store, overriding the "
                'built-in preset. kind is one of link|pdf|image.'
            ),
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="remove every resource button from this tenant.",
        )

    # -- helpers -----------------------------------------------------------

    def _resolve_tenant(self, needle: str) -> Tenant:
        matches = list(
            Tenant.objects.filter(
                Q(name__icontains=needle) | Q(slug__icontains=needle)
            ).order_by("id")
        )
        if not matches:
            raise CommandError(f"No tenant matches {needle!r}.")
        if len(matches) > 1:
            for t in matches:
                self.stdout.write(f"  [{t.id}] {t.name!r} / {t.slug!r}")
            raise CommandError(f"{len(matches)} tenants match {needle!r}.")
        return matches[0]

    def _desired(self, needle: str, raw_json: str, clear: bool) -> list[dict]:
        if clear:
            return []
        if raw_json:
            try:
                parsed = json.loads(raw_json)
            except ValueError as exc:
                raise CommandError(f"--resources is not valid JSON: {exc}") from exc
            if not isinstance(parsed, list):
                raise CommandError("--resources must be a JSON array.")
            cleaned = normalize_checkin_resources(parsed)
            # Refuse a silent partial write. Dropping a row here means a typo'd
            # url or a missing label, and seeding 1 of 2 buttons without saying
            # so is how a brand ends up half-configured in the field.
            if len(cleaned) != len(parsed):
                raise CommandError(
                    f"{len(parsed) - len(cleaned)} of {len(parsed)} entries were "
                    "rejected (need a label, a kind of link|pdf|image, and an "
                    "http(s) or root-relative url). Nothing written."
                )
            return cleaned

        key = next((k for k in PRESETS if k in needle.lower()), None)
        if key is None:
            raise CommandError(
                f"No built-in preset for {needle!r} — pass --resources '<json>' "
                f"or --clear. Presets: {', '.join(sorted(PRESETS))}."
            )
        return normalize_checkin_resources(PRESETS[key])

    def _show(self, title: str, resources: list[dict]) -> None:
        self.stdout.write(f"{title} ({len(resources)}):")
        if not resources:
            self.stdout.write("    (none)")
            return
        for r in resources:
            note = f"  — {r['note']}" if r.get("note") else ""
            self.stdout.write(f"    · [{r['kind']}] {r['label']}{note}")
            self.stdout.write(f"        {r['url']}")

    # -- main --------------------------------------------------------------

    def handle(self, *args, **opts):
        needle = (opts["tenant"] or "").strip()
        apply = bool(opts["apply"])
        clear = bool(opts["clear"])
        raw_json = (opts["resources"] or "").strip()

        if clear and raw_json:
            raise CommandError("Pass --clear or --resources, not both.")

        tenant = self._resolve_tenant(needle)
        desired = self._desired(needle, raw_json, clear)
        current = normalize_checkin_resources(
            getattr(tenant, "checkin_resources", None)
        )

        self.stdout.write(f"Tenant     : [{tenant.id}] {tenant.name!r}")
        self.stdout.write(
            f"Check-in   : {tenant.checkin_code or '(no standing code)'}"
        )
        if tenant.checkin_code:
            self.stdout.write(
                f"Link       : {ASSET_BASE}/checkin/{tenant.checkin_code}"
            )
        # The legacy field still feeds the event-confirmation "Training site"
        # line, so print it — it explains any card that shows up unbidden.
        self.stdout.write(
            f"Legacy url : {tenant.checkin_training_url or '(unset)'}"
        )
        self.stdout.write("")
        self._show("Current", current)
        self.stdout.write("")
        self._show("Desired", desired)

        if len(desired) > MAX_CHECKIN_RESOURCES:
            raise CommandError(
                f"{len(desired)} resources exceeds the {MAX_CHECKIN_RESOURCES} "
                "the page will render."
            )

        if current == desired:
            self.stdout.write("")
            self.stdout.write(
                self.style.SUCCESS("Already set — nothing to do.")
            )
            return

        if not apply:
            self.stdout.write("")
            self.stdout.write("DRY RUN — re-run with --apply to write.")
            return

        with transaction.atomic():
            # Store None rather than [] when clearing, so the model falls back
            # to `checkin_training_url` exactly as an untouched tenant does.
            tenant.checkin_resources = desired or None
            tenant.save(update_fields=["checkin_resources"])

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(desired)} resource(s) to {tenant.name!r}."
            )
        )
