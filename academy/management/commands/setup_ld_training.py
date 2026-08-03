"""Seed (or re-seed) the Liquid Death BA training hub.

Mints the shareable ``/training/<code>`` link and its four resources. Dry-run
by default like every other seeder here — ``--apply`` writes.

Idempotent on two keys: the hub is matched by (tenant, title) so re-running
never mints a second code for the same campaign, and each resource is matched
by (hub, title) so editing copy or swapping a URL updates in place instead of
duplicating the card. That matters because this command is wired to a
workflow_dispatch endpoint: the safe assumption is that it gets run again.

The asset URLs are relative on purpose. They resolve against whatever host
serves the page, so the same rows work on admin.igniteproductions.co and on a
preview channel — and the files themselves ship in the front-end repo under
``public/training/ld/``, which means the link has no dependency on GCS
credentials or a third-party video host.
"""

from __future__ import annotations

import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from academy.models import TrainingHub, TrainingResource
from tenants.models import Tenant

CODE_PREFIX = "LD-"
# No 0/O/1/I/L — the code gets read aloud and retyped off a text message.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

HUB_TITLE = "Liquid Death — BA Training"
HUB_SUBTITLE = "Everything you need before your first shift"
# No count in the copy on purpose — it silently went stale the first time a
# resource was added, and the page already numbers the cards.
HUB_INTRO = (
    "Work through these in order. The field guide and the video cover how we "
    "run a sampling shift; the product guide and FAQ are the ones you'll want "
    "open on your phone at the table."
)

# (order, kind, title, description, url, meta_label)
RESOURCES: list[tuple[int, str, str, str, str, str]] = [
    (
        10,
        "page",
        "Retail Sampling Guide",
        "The field guide: how to set up, how to pitch, what to wear, how to "
        "handle objections, and what a great recap looks like.",
        "/training/ld/field-guide.html",
        "Read first",
    ),
    (
        20,
        "video",
        "BA Training Video",
        "Walkthrough of the handbook — the shift start to finish.",
        "/training/ld/training-video.mp4",
        "8 min",
    ),
    (
        30,
        "pdf",
        "LD Energy Product Guide",
        "Every Sparkling Energy SKU: flavors, caffeine, and the talking "
        "points that actually land with shoppers.",
        "/training/ld/product-guide.pdf",
        "Sell sheets",
    ),
    (
        40,
        "pdf",
        "BA FAQ",
        "The questions BAs ask us most — pay, gear, parking, what to do when "
        "the store doesn't know you're coming.",
        "/training/ld/ba-faq.pdf",
        "FAQ",
    ),
    (
        50,
        "link",
        "Liquid Death — Brand Overview",
        "The brand's own site. Skim it so you can talk about Liquid Death "
        "the way Liquid Death does.",
        "https://liquiddeath.com",
        "Website",
    ),
]


class Command(BaseCommand):
    help = "Seed the Liquid Death shareable BA training hub (dry-run by default)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="liquid death",
            help="tenant name/slug substring (case-insensitive). "
            "Default: 'liquid death'.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write. Without this the command only reports.",
        )
        parser.add_argument(
            "--code",
            default="",
            help="force a specific hub code instead of generating one. "
            "Use to re-point an existing printed/texted link.",
        )
        parser.add_argument(
            "--video-url",
            default="",
            help="override the video resource URL — e.g. an unlisted YouTube "
            "link if the self-hosted MP4 is ever replaced.",
        )

    # -- tenant resolution -------------------------------------------------

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
                self.stdout.write(f"  [{t.id}] name={t.name!r} slug={t.slug!r}")
            raise CommandError(
                f"{len(matches)} tenants match {needle!r} "
                f"({', '.join(repr(t.slug) for t in matches)}) — narrow --tenant."
            )
        return matches[0]

    def _mint_code(self) -> str:
        for _ in range(50):
            candidate = CODE_PREFIX + "".join(
                secrets.choice(ALPHABET) for _ in range(6)
            )
            if not TrainingHub.objects.filter(code=candidate).exists():
                return candidate
        raise CommandError("Could not mint an unused training code.")

    # -- main --------------------------------------------------------------

    def handle(self, *args, **opts):
        tenant = self._resolve_tenant(opts["tenant"].strip())
        apply = bool(opts["apply"])
        forced_code = (opts["code"] or "").strip().upper()
        video_override = (opts["video_url"] or "").strip()

        self.stdout.write(
            f"Tenant : [{tenant.id}] {tenant.name!r} (slug {tenant.slug!r})"
        )

        existing = TrainingHub.objects.filter(
            tenant=tenant, title=HUB_TITLE
        ).first()
        if existing:
            self.stdout.write(
                f"Hub    : EXISTS [{existing.id}] code={existing.code} "
                f"active={existing.is_active} "
                f"resources={existing.resources.count()}"
            )
        else:
            self.stdout.write("Hub    : none yet — will be created")

        if not apply:
            self.stdout.write("")
            self.stdout.write("DRY RUN — would ensure these resources:")
            for order, kind, title, _desc, url, meta in RESOURCES:
                shown = video_override if (kind == "video" and video_override) else url
                self.stdout.write(f"  {order:>3}  {kind:<5}  {title}  →  {shown}")
            self.stdout.write("")
            self.stdout.write("Re-run with --apply to write.")
            return

        with transaction.atomic():
            if existing:
                hub = existing
                if forced_code and hub.code != forced_code:
                    hub.code = forced_code
                hub.subtitle = HUB_SUBTITLE
                hub.intro = HUB_INTRO
                hub.is_active = True
                hub.save()
                created_hub = False
            else:
                hub = TrainingHub.objects.create(
                    tenant=tenant,
                    code=forced_code or self._mint_code(),
                    title=HUB_TITLE,
                    subtitle=HUB_SUBTITLE,
                    intro=HUB_INTRO,
                    is_active=True,
                )
                created_hub = True

            touched = 0
            for order, kind, title, desc, url, meta in RESOURCES:
                final_url = (
                    video_override if (kind == "video" and video_override) else url
                )
                TrainingResource.objects.update_or_create(
                    hub=hub,
                    title=title,
                    defaults={
                        "order": order,
                        "kind": kind,
                        "description": desc,
                        "url": final_url,
                        "meta_label": meta,
                        "published": True,
                    },
                )
                touched += 1

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created_hub else 'Updated'} hub "
                f"[{hub.id}] code={hub.code} — {touched} resources ensured."
            )
        )
        self.stdout.write(
            f"Link   : https://admin.igniteproductions.co/training/{hub.code}"
        )
