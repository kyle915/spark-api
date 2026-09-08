"""One-shot backlog: approve filed Feel Free walk-up recaps (admin tool).

Feel Free walk-ups require Ignite admin approval before client send /
visibility — do not re-enable submit-time auto-approve. This command exists
only to stamp already-filed unapproved rows (e.g. after a mistaken
auto-approve rollback). Dry-run by default; leave ``apply`` for a human.

    uv run python manage.py approve_feel_free_walkup_recaps --since=2026-08-23
    uv run python manage.py approve_feel_free_walkup_recaps --since=2026-08-23 --apply
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef
from django.utils import timezone

from ambassadors.checkin_web import is_feel_free_tenant
from ambassadors.walkup import approve_booking_for_recap
from recaps.filed import custom_filed_q
from recaps.models import CustomRecap, CustomRecapFile
from tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Approve filed Feel Free walk-up custom recaps that are still "
        "unapproved (dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            required=True,
            help="Pacific calendar date YYYY-MM-DD (inclusive). Use 2026-08-23 for #996.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write approvals. Without this flag, only list candidates.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max rows to approve (0 = no cap).",
        )

    def handle(self, *args, **opts):
        since_raw = (opts["since"] or "").strip()
        try:
            since_day = datetime.strptime(since_raw, "%Y-%m-%d").date()
        except ValueError as exc:
            raise CommandError("--since must be YYYY-MM-DD") from exc

        pacific = ZoneInfo("America/Los_Angeles")
        since_dt = timezone.make_aware(
            datetime.combine(since_day, time.min), timezone=pacific
        )
        apply = bool(opts["apply"])
        limit = int(opts["limit"] or 0)

        tenants = [
            t
            for t in Tenant.objects.all().only(
                "id", "name", "slug", "request_url_name"
            )
            if is_feel_free_tenant(t)
        ]
        if not tenants:
            self.stdout.write("No Feel Free tenant matched.")
            return

        tenant_ids = [t.id for t in tenants]
        has_file = Exists(
            CustomRecapFile.objects.filter(custom_recap_id=OuterRef("pk"))
        )
        qs = (
            CustomRecap.objects.filter(
                tenant_id__in=tenant_ids,
                approved=False,
                submitted_at__gte=since_dt,
            )
            .filter(custom_filed_q())
            .filter(has_file)
            .select_related("ambassador", "event", "created_by")
            .order_by("id")
        )
        if limit > 0:
            qs = qs[:limit]

        rows = list(qs)
        self.stdout.write(
            f"Mode={'APPLY' if apply else 'DRY-RUN'} · since={since_day} · "
            f"candidates={len(rows)} · tenants={[t.name for t in tenants]}"
        )
        if not rows:
            return

        approved = 0
        for recap in rows:
            actor = recap.created_by
            self.stdout.write(
                f"  #{recap.id} · {recap.name!r} · event={getattr(recap.event, 'name', None)!r} · "
                f"submitted={recap.submitted_at}"
            )
            if not apply:
                continue
            now = timezone.now()
            recap.approved = True
            recap.approved_by = actor
            recap.approved_at = now
            recap.updated_by = actor
            recap.save(
                update_fields=[
                    "approved",
                    "approved_by",
                    "approved_at",
                    "updated_by",
                    "updated_at",
                ]
            )
            if recap.ambassador_id and recap.event_id:
                try:
                    approve_booking_for_recap(
                        ambassador_id=recap.ambassador_id,
                        event_id=recap.event_id,
                        actor=actor,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(
                        f"    booking hours failed for #{recap.id}: {exc}"
                    )
            approved += 1

        if apply:
            self.stdout.write(self.style.SUCCESS(f"Approved {approved} Feel Free recap(s)."))
        else:
            self.stdout.write(
                "Dry-run only. Re-run with --apply to stamp approved=True."
            )
