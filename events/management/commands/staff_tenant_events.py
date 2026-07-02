"""Bulk-staff a tenant's imported events: BAs + jobs + approved bookings.

After a season import (import_event_schedule) the events exist but have no
jobs and no staff. This command reads a committed staffing spec and, per
market group:

  1. Ensures each BA exists — reuses the admin Add-a-BA service
     (PublicAmbassadorCreationService with CreateAmbassadorWithUserInput),
     so a NEW BA gets the exact same treatment as the web UI's flow:
     generated password, verified UserStatus, active Ambassador profile,
     and the "Welcome to Spark by Ignite" email with the app-store
     download buttons. Existing users just gain/reactivate a BA profile
     and get NO email.
  2. Ensures each matched event has a Job (hourly rate + shift hours from
     the spec; lifecycle "filled" when BAs are attached, "pending" when
     the group has none yet).
  3. Books every group BA on every group event: AmbassadorJob (accepted
     status + hourly Rate row) + the canonical _ensure_approved_booking
     AmbassadorEvent write — direct ORM, so bulk staffing does NOT fan
     out per-booking pushes/emails.

Specs live in events/management/commands/data/<key>.json:
    {"tenant_name", "event_type", "hourly_rate", "total_hours",
     "groups": [{event_name_prefix, bas: [{first_name, last_name,
                 email, phone}]}]}

Dry-run by default — reports matched events + which BAs would be created
(and therefore emailed). Pass --apply to write. Prod: secret-gated
StaffTenantEventsView (/internal/cron/staff-tenant-events) + the
staff-tenant-events workflow.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ambassadors.models import Ambassador
from events.models import Event
from tenants.models import Tenant

User = get_user_model()

_DATA_DIR = Path(__file__).resolve().parent / "data"
_KEY_RE = re.compile(r"^[a-z0-9_]+$")


class Command(BaseCommand):
    help = (
        "Bulk-staff a tenant's events from a committed spec: ensure BAs "
        "(new ones get the welcome/app-download email), create jobs, book "
        "every group BA on every group event. Dry-run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--spec",
            default="feel_free_staffing",
            help="Spec key → events/management/commands/data/<key>.json",
        )
        parser.add_argument(
            "--owner-email",
            default="kyle@igniteproductions.co",
            help="Admin recorded as created_by on jobs/bookings.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (and email NEW BAs). Dry-run without it.",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        key = (opts["spec"] or "").strip().lower()
        if not _KEY_RE.match(key):
            raise CommandError(f"Invalid --spec key: {key!r}")
        path = _DATA_DIR / f"{key}.json"
        if not path.exists():
            raise CommandError(f"Spec file not found: {path}")
        spec = json.loads(path.read_text())

        tenant = (
            Tenant.objects.filter(name__iexact=(spec.get("tenant_name") or "").strip())
            .order_by("id")
            .first()
        )
        if not tenant:
            raise CommandError(f"Tenant not found: {spec.get('tenant_name')!r}")
        owner = (
            User.objects.filter(email__iexact=opts["owner_email"]).order_by("id").first()
        )
        if not owner:
            raise CommandError(f"Owner user not found: {opts['owner_email']}")

        event_type = (spec.get("event_type") or "").strip()
        hourly_rate = Decimal(str(spec.get("hourly_rate") or "0"))
        total_hours = Decimal(str(spec.get("total_hours") or "0"))

        w = self.stdout.write
        w("")
        w(self.style.MIGRATE_HEADING(f"Staff tenant events: {key}"))
        w(f"  mode   : {'APPLY (writing)' if apply else 'DRY-RUN (no writes, no emails)'}")
        w(f"  tenant : {tenant.id} ({tenant.name})")
        w(f"  owner  : {owner.id} ({owner.email})")
        w(f"  rate   : ${hourly_rate}/hr · {total_hours}h/shift")

        status_accept = rate_row = job_title = None
        if apply:
            status_accept, rate_row, job_title = self._ensure_job_refs(
                tenant, owner, hourly_rate
            )

        totals = {"events": 0, "jobs_created": 0, "ba_created": 0,
                  "ba_existing": 0, "assignments": 0, "bookings": 0}

        for group in spec.get("groups") or []:
            prefix = group.get("event_name_prefix") or ""
            bas = group.get("bas") or []
            events = list(
                Event.objects.filter(
                    tenant=tenant,
                    event_type__name__iexact=event_type,
                    name__startswith=prefix,
                ).order_by("start_time")
            )
            totals["events"] += len(events)
            w("")
            w(self.style.NOTICE(
                f"[{prefix.rstrip(' —')}] {len(events)} event(s), {len(bas)} BA(s)"
            ))

            ambassadors = []
            for ba in bas:
                amb, created_user = self._ensure_ba(ba, apply=apply)
                label = f"{ba['first_name']} {ba.get('last_name', '')}".strip()
                if created_user:
                    totals["ba_created"] += 1
                    w(f"  + {label} <{ba['email']}> — "
                      f"{'created + welcome email sent' if apply else 'WOULD create + send welcome email'}")
                else:
                    totals["ba_existing"] += 1
                    w(f"  · {label} <{ba['email']}> — existing user"
                      + ("" if amb else " (no BA profile yet)"))
                if amb:
                    ambassadors.append(amb)

            if not apply:
                continue

            from jobs.models import AmbassadorJob, Job
            from jobs.mutations import _ensure_approved_booking

            for event in events:
                job, job_created = Job.objects.get_or_create(
                    event=event,
                    defaults={
                        "tenant": tenant,
                        "job_title": job_title,
                        "rate": rate_row,
                        "name": event.name,
                        "description": event.notes or "",
                        "address": event.address or "",
                        "start_date": event.start_time,
                        "end_date": event.end_time,
                        "hourly_rate": hourly_rate,
                        "total_hours": total_hours,
                        "lifecycle_status": (
                            Job.STATUS_FILLED if ambassadors else Job.STATUS_PENDING
                        ),
                        "public": False,
                        "favorites_only": True,
                        "created_by": owner,
                        "updated_by": owner,
                    },
                )
                if job_created:
                    totals["jobs_created"] += 1
                else:
                    # Self-heal jobs from earlier runs: the GraphQL JobType
                    # declares rate non-null, so a rate-less Job breaks the
                    # whole admin JobsQuery.
                    patch = []
                    if job.rate_id is None:
                        job.rate = rate_row
                        patch.append("rate")
                    if job.hourly_rate is None:
                        job.hourly_rate = hourly_rate
                        job.total_hours = total_hours
                        patch += ["hourly_rate", "total_hours"]
                    if patch:
                        job.save(update_fields=patch + ["updated_at"])

                for amb in ambassadors:
                    _, aj_created = AmbassadorJob.objects.get_or_create(
                        ambassador=amb,
                        job=job,
                        defaults={
                            "tenant": tenant,
                            "status": status_accept,
                            "rate": rate_row,
                            "accepted_terms": False,
                            "appear_as_rfp": False,
                            "created_by": owner,
                            "updated_by": owner,
                        },
                    )
                    if aj_created:
                        totals["assignments"] += 1
                    action = _ensure_approved_booking(
                        ambassador_id=amb.id,
                        event_id=event.id,
                        tenant_id=tenant.id,
                        creator_id=owner.id,
                    )
                    if action in ("created", "approved"):
                        totals["bookings"] += 1

        w("")
        w(self.style.SUCCESS(
            f"Done. events={totals['events']} jobs_created={totals['jobs_created']} "
            f"ba_created={totals['ba_created']} ba_existing={totals['ba_existing']} "
            f"assignments={totals['assignments']} bookings={totals['bookings']}"
        ))
        if not apply:
            w(self.style.MIGRATE_LABEL(
                "DRY-RUN — nothing written, no emails. Re-run with --apply "
                "(execute=true)."
            ))

    # ----- helpers -----

    def _ensure_ba(self, ba: dict, *, apply: bool):
        """Returns (ambassador_or_None, created_user: bool). In dry-run,
        never creates anything — created_user just reports intent."""
        email = (ba.get("email") or "").strip()
        user = User.objects.filter(email__iexact=email).order_by("id").first()
        if user is None:
            if not apply:
                return None, True
            from ambassadors.inputs import CreateAmbassadorWithUserInput
            from ambassadors.services import PublicAmbassadorCreationService

            resp = async_to_sync(PublicAmbassadorCreationService.create)(
                input=CreateAmbassadorWithUserInput(
                    first_name=(ba.get("first_name") or "").strip(),
                    last_name=(ba.get("last_name") or "").strip() or None,
                    email=email,
                    phone=(ba.get("phone") or "").strip() or None,
                    password1=None,
                    password2=None,
                ),
                info=None,
                ambassador_is_active=True,
            )
            if not getattr(resp, "success", False):
                raise CommandError(
                    f"BA creation failed for {email}: {getattr(resp, 'message', '?')}"
                )
            amb = Ambassador.objects.filter(user__email__iexact=email).first()
            return amb, True

        amb = Ambassador.objects.filter(user=user).first()
        if amb is None and apply:
            amb, _ = Ambassador.objects.get_or_create(
                user=user,
                defaults={
                    "phone": (ba.get("phone") or "").strip() or None,
                    "is_active": True,
                    "created_by": user,
                    "updated_by": user,
                },
            )
        if amb is not None and not amb.is_active and apply:
            amb.is_active = True
            amb.save(update_fields=["is_active"])
        return amb, False

    def _ensure_job_refs(self, tenant, owner, hourly_rate):
        """Accepted Status + hourly Rate + JobTitle rows the FKs need."""
        from jobs.models import JobTitle, Rate, RateType, Status

        job_title = (
            JobTitle.objects.filter(tenant=tenant).order_by("id").first()
        )
        if job_title is None:
            job_title = JobTitle.objects.create(
                tenant=tenant, name="Brand Ambassador",
                created_by=owner, updated_by=owner,
            )

        status = (
            Status.objects.filter(tenant=tenant, name__icontains="accept")
            .order_by("id")
            .first()
        )
        if status is None:
            status = Status.objects.create(
                tenant=tenant, name="Accepted", created_by=owner, updated_by=owner,
            )
        rate_type = (
            RateType.objects.filter(tenant=tenant, name__icontains="hour")
            .order_by("id")
            .first()
        )
        if rate_type is None:
            rate_type = RateType.objects.create(
                tenant=tenant, name="Hourly", created_by=owner, updated_by=owner,
            )
        rate = (
            Rate.objects.filter(
                tenant=tenant, rate_type=rate_type, amount=hourly_rate
            )
            .order_by("id")
            .first()
        )
        if rate is None:
            rate = Rate.objects.create(
                tenant=tenant, rate_type=rate_type, amount=hourly_rate,
                created_by=owner, updated_by=owner,
            )
        return status, rate, job_title
