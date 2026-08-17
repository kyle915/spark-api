"""List every live walk-up check-in link and what each one is wired to.

There was no way to answer "which brands have a standing link, and is each one
set up correctly?" without opening tenants one at a time. That matters because
the failure modes here are all SILENT — a link with no pinned event type hands
BAs an arbitrary recap form, and a configured photo bucket with no category row
is skipped at render with nothing said.

Read-only. Nothing is written, so this is safe to run against prod any time.

For each tenant with a `checkin_code` it prints the URL, the pinned program and
the recap form that program resolves to, the photo buckets the page will
actually serve, and a PROBLEMS line naming anything that will misbehave.

Usage::

    python manage.py list_checkin_links
    python manage.py list_checkin_links --tenant-id 18
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from tenants.models import Tenant

BASE_URL = "https://client.igniteproductions.co"


class Command(BaseCommand):
    help = "List every tenant's standing check-in link and its wiring (read-only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            dest="tenant_id",
            type=int,
            default=None,
            help="Only this tenant. Default: every tenant with a code.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        from events.models import Event
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory

        qs = Tenant.objects.exclude(checkin_code__isnull=True).exclude(
            checkin_code=""
        )
        if opts["tenant_id"]:
            qs = qs.filter(id=opts["tenant_id"])
        tenants = list(qs.order_by("id"))

        self.stdout.write("=" * 72)
        self.stdout.write(f"WALK-UP CHECK-IN LINKS — {len(tenants)} live")
        self.stdout.write("=" * 72)

        if not tenants:
            self.stdout.write(
                "\nNo tenant has a checkin_code. Mint one with setup_tenant_checkin."
            )
            return

        total_problems = 0
        for t in tenants:
            problems: list[str] = []
            self.stdout.write("")
            self.stdout.write("-" * 72)
            self.stdout.write(f"[{t.id}] {t.name}")
            self.stdout.write(f"  {BASE_URL}/checkin/{t.checkin_code}")
            recap = (getattr(t, "checkin_recap_code", None) or "").strip()
            if recap:
                self.stdout.write(
                    f"  recap-only   : {BASE_URL}/checkin/{recap}  (no time clock)"
                )
            self.stdout.write(
                f"  location mode : {t.checkin_location_mode}"
                + ("  (BA types a store address)" if t.checkin_location_mode == "address" else "  (BA picks a market)")
            )

            # -- programs ---------------------------------------------
            pinned = t.checkin_event_type
            selectable = list(t.checkin_event_types.all())
            if pinned is None:
                problems.append(
                    "NO PINNED EVENT TYPE — the walk-in path falls back to the "
                    "tenant's lowest-id type, so BAs may get the wrong recap form"
                )
                self.stdout.write("  program       : (none pinned)")
            else:
                self.stdout.write(f"  program       : {pinned.name}")
            if len(selectable) > 1:
                self.stdout.write(
                    "  selectable    : "
                    + ", ".join(e.name for e in selectable)
                    + "   (BA is asked which)"
                )

            # -- the recap form each program opens --------------------
            programs = selectable or ([pinned] if pinned else [])
            for etype in programs:
                tpl = CustomRecapTemplate.objects.filter(
                    tenant=t, event_type_id=etype.id
                ).first()
                if tpl is None:
                    problems.append(
                        f"{etype.name!r} has NO RECAP TEMPLATE — BAs get the "
                        "generic form"
                    )
                    self.stdout.write(f"  form          : {etype.name} -> (none)")
                else:
                    n = CustomField.objects.filter(
                        custom_recap_template=tpl
                    ).count()
                    self.stdout.write(
                        f"  form          : {etype.name} -> [{tpl.id}] {tpl.name} "
                        f"({n} fields)"
                    )

            # -- photo buckets, as the page will actually serve them ---
            # Resolved through the real serializer rather than read off the
            # JSON, because a configured bucket whose category row is missing
            # is silently DROPPED at render — reading the config alone would
            # report buckets the BA never sees.
            from ambassadors.checkin_web import (
                photo_bucket_specs,
                serialize_photo_buckets,
            )

            if not t.checkin_photo_buckets:
                self.stdout.write(
                    "  photos        : one generic grid (no labelled buckets)"
                )
            else:
                for etype in programs:
                    served = serialize_photo_buckets(
                        Event(tenant=t, event_type=etype)
                    )
                    configured = [
                        e.get("name")
                        for e in photo_bucket_specs(t, etype)
                        if isinstance(e, dict)
                    ]
                    self.stdout.write(
                        f"  photos        : {etype.name} -> "
                        + (
                            " | ".join(b["name"] for b in served)
                            if served
                            else "(none served)"
                        )
                    )
                    if len(served) != len(configured):
                        missing = len(configured) - len(served)
                        problems.append(
                            f"{etype.name!r}: {missing} configured photo "
                            "bucket(s) have no category row and are SKIPPED"
                        )

            n_cats = FileRecapCategory.objects.filter(tenant_id=t.id).count()
            self.stdout.write(f"  categories    : {n_cats} on this tenant")

            if t.checkin_training_url:
                self.stdout.write(f"  training      : {t.checkin_training_url}")
            resources = t.checkin_resources or []
            if resources:
                self.stdout.write(
                    "  resources     : "
                    + ", ".join(
                        str(r.get("label")) for r in resources if isinstance(r, dict)
                    )
                )

            if problems:
                total_problems += len(problems)
                for p in problems:
                    self.stdout.write(self.style.WARNING(f"  ! {p}"))
            else:
                self.stdout.write(self.style.SUCCESS("  OK — no problems found"))

        self.stdout.write("")
        self.stdout.write("=" * 72)
        if total_problems:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(tenants)} link(s), {total_problems} problem(s) above."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"{len(tenants)} link(s), all wired correctly.")
            )
        self.stdout.write("Read-only — nothing was modified.")
        self.stdout.write("=" * 72)
