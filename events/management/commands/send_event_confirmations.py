"""Wall-clock sweep: fire the 24h and 3h event-confirmation reminders.

There is NO Redis / rqscheduler on Cloud Run, so "send this in 24 hours" cannot
be a scheduled job — every django-rq ``scheduler.schedule(...)`` in this repo's
history was silently dropped. Like the activation reminder, the recap nudge and
the ambassador-job reminders, this is a periodic scan instead: a GitHub Actions
cron hits ``/internal/cron/send-event-confirmations`` every ~15 minutes and the
email goes out INLINE in the web process.

The "booked" stage is NOT swept — it's only ever sent by an admin pressing Send
in the tab. This command sends reminders and nothing else.

Safety, in the order it matters:

* **Idempotent.** Each (confirmation, stage) is claimed in
  ``EventConfirmationSend`` before its email is handed to the driver, under a
  unique constraint — so overlapping runs can't double-send. See
  ``events.event_confirmations.send_confirmation_stage``.
* **Opt-in.** Only rows an admin sent with reminders enabled are eligible
  (``send_reminders=True``). A confirmation nobody created stays silent, so this
  sweep can never reach the whole roster.
* **Bounded.** Past shifts, cancelled rows, and stages whose moment predates the
  booking are all excluded, and ``--max-sends`` caps a single run.
* **Timezone-correct.** Windows are computed against each confirmation's aware
  ``starts_at``, never ``timezone.localdate()`` — ``settings.TIME_ZONE`` is UTC,
  so a local-date comparison flips a day early at 5pm Pacific.

Fully synchronous on purpose: ``asyncio.run()`` inside a cron endpoint deadlocks
under this setup, so nothing on this path is async.

``--preview-to`` is the odd one out: it sends ONE sample confirmation to an
address you name and touches neither the database nor any real BA. It exists
because the send path can't be proven from a laptop — the Resend key lives on
Cloud Run — so this is how you confirm ``staffing@igniteproductions.co`` is a
verified sender and that the email renders in a real client, from the same code
production uses.

Usage:
    python manage.py send_event_confirmations --dry-run
    python manage.py send_event_confirmations
    python manage.py send_event_confirmations --tenant-id 1 --max-sends 25
    python manage.py send_event_confirmations --preview-to you@example.com
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)

# A single run should never legitimately need to send more than this. Hitting
# the cap means something is wrong (a backfill, a clock jump), so we stop and
# say so rather than emailing everyone.
DEFAULT_MAX_SENDS = 100


class Command(BaseCommand):
    help = (
        "Send due 24h/3h event-confirmation reminders. Run every ~15 min from "
        "a cron runner. Use --dry-run to see what would fire."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be sent; send nothing and claim nothing.",
        )
        parser.add_argument(
            "--grace-hours",
            type=int,
            default=6,
            help=(
                "How stale a due reminder may be before it's dropped (default "
                "6). Covers cron downtime without firing a '24 hours before' "
                "email an hour before the shift."
            ),
        )
        parser.add_argument(
            "--tenant-id",
            type=int,
            default=None,
            help="Restrict to a single tenant.",
        )
        parser.add_argument(
            "--max-sends",
            type=int,
            default=DEFAULT_MAX_SENDS,
            help=f"Stop after this many sends in one run (default {DEFAULT_MAX_SENDS}).",
        )
        parser.add_argument(
            "--preview-to",
            type=str,
            default=None,
            help=(
                "Send ONE sample confirmation to this address and exit. Writes "
                "nothing and touches no real BA — use it to verify the sender "
                "domain and rendering."
            ),
        )
        parser.add_argument(
            "--preview-stage",
            type=str,
            default="booked",
            choices=["booked", "t24", "t3", "all"],
            help="Which stage to preview (default booked; 'all' sends all three).",
        )
        parser.add_argument(
            "--preview-tenant-id",
            type=int,
            default=None,
            help=(
                "Build the preview from this tenant's real name + recap/training "
                "links. Defaults to a built-in Liquid Death sample."
            ),
        )

    def handle(self, *args, **opts):
        from events.event_confirmations import due_reminders, send_confirmation_stage

        if opts.get("preview_to"):
            return self._preview(opts)

        dry = bool(opts["dry_run"])
        grace = max(1, int(opts["grace_hours"]))
        tenant_id = opts.get("tenant_id")
        max_sends = max(1, int(opts["max_sends"]))

        now = timezone.now()
        self.stdout.write(
            f"send_event_confirmations: now={now.isoformat()} dry_run={dry} "
            f"grace_hours={grace} tenant_id={tenant_id or 'all'} "
            f"max_sends={max_sends}"
        )

        due = due_reminders(now=now, grace_hours=grace)
        if tenant_id:
            due = [(c, s) for c, s in due if c.tenant_id == int(tenant_id)]

        if not due:
            self.stdout.write("Nothing due. 0 sent.")
            return

        capped = len(due) > max_sends
        if capped:
            # Never truncate silently — a run that quietly dropped half its work
            # reads as "all clear" in the Actions log.
            self.stdout.write(
                self.style.WARNING(
                    f"{len(due)} reminders due but --max-sends is {max_sends}. "
                    f"Sending the {max_sends} soonest; {len(due) - max_sends} "
                    f"deferred to the next run. Investigate if this repeats."
                )
            )
            due.sort(key=lambda pair: pair[0].starts_at)
            due = due[:max_sends]

        sent = 0
        failed = 0
        skipped = 0
        for confirmation, stage in due:
            label = (
                f"stage={stage} confirmation={confirmation.id} "
                f"tenant={confirmation.tenant_id} to={confirmation.ba_email} "
                f"starts={confirmation.starts_at.isoformat()}"
            )
            if dry:
                self.stdout.write(f"[dry-run] would send {label}")
                continue

            result = send_confirmation_stage(confirmation, stage)
            if result.sent:
                sent += 1
                self.stdout.write(f"sent {label}")
            elif result.reason in ("already-sent", "attempts-exhausted", "no-email"):
                skipped += 1
                self.stdout.write(f"skipped ({result.reason}) {label}")
            else:
                failed += 1
                self.stdout.write(
                    self.style.ERROR(f"FAILED ({result.reason}) {label}")
                )

        if dry:
            self.stdout.write(f"[dry-run] {len(due)} reminder(s) would fire. 0 sent.")
            return

        summary = (
            f"event confirmations: sent {sent}, skipped {skipped}, "
            f"failed {failed}, of {len(due)} due."
        )
        self.stdout.write(
            self.style.ERROR(summary) if failed else self.style.SUCCESS(summary)
        )

    def _preview(self, opts) -> None:
        """Send a sample confirmation to one address. Writes nothing.

        The confirmation is built IN MEMORY (never saved), so this can't create
        a row the sweep would later pick up, and it can't stamp a real BA's
        ledger. It goes through the normal Mailer, so the logo attachment, the
        plain-text alternative and the deliverability headers are all exercised
        exactly as a live send would be.
        """
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        from events.event_confirmations import (
            EventConfirmationMailer, build_subject,
        )
        from events.models import EventConfirmation, TimeZone
        from tenants.models import Tenant

        to_email = str(opts["preview_to"]).strip()
        if "@" not in to_email:
            self.stderr.write(self.style.ERROR(f"Not an email: {to_email!r}"))
            return

        tenant_id = opts.get("preview_tenant_id")
        tenant = Tenant.objects.filter(id=tenant_id).first() if tenant_id else None
        if tenant is None:
            # Unsaved stand-in so a preview works even before a tenant is set up.
            tenant = Tenant(
                name="Liquid Death",
                checkin_code="LD-TNBJ8K",
                checkin_training_url=(
                    "https://admin.igniteproductions.co/training/LD-FZUWXT"
                ),
            )

        zone = ZoneInfo("America/Chicago")
        start = (timezone.now().astimezone(zone) + timedelta(days=4)).replace(
            hour=13, minute=0, second=0, microsecond=0
        )
        sample = EventConfirmation(
            tenant=tenant,
            timezone=TimeZone(name="America/Chicago", code="CT", offset=-300),
            ba_name="Sample BA",
            ba_email=to_email,
            store_name="Jewel Osco",
            address="4042 W Foster Ave, Chicago, IL 60630, USA",
            event_type_label="Retail Sampling",
            starts_at=start,
            ends_at=start + timedelta(hours=3),
            products=[
                "Sparkling Water — Squeezed-to-Death",
                "Sparkling Water — Severed Lime",
                "Sparkling Water — Rootbeer Wrath",
            ],
        )

        choice = str(opts.get("preview_stage") or "booked")
        stages = (
            [
                EventConfirmation.STAGE_BOOKED,
                EventConfirmation.STAGE_T24,
                EventConfirmation.STAGE_T3,
            ]
            if choice == "all"
            else [choice]
        )

        self.stdout.write(
            f"preview -> {to_email} | tenant={tenant.name!r} "
            f"| subject={build_subject(sample)!r}"
        )
        ok = 0
        for stage in stages:
            try:
                EventConfirmationMailer(sample, stage).send_now()
            except Exception as exc:  # noqa: BLE001 — report, don't traceback
                self.stdout.write(
                    self.style.ERROR(f"  {stage}: FAILED — {exc}")
                )
                continue
            ok += 1
            self.stdout.write(self.style.SUCCESS(f"  {stage}: sent"))

        if ok != len(stages):
            self.stdout.write(
                self.style.ERROR(
                    f"preview: {ok}/{len(stages)} sent. A rejected send usually "
                    f"means the FROM domain isn't verified in Resend."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"preview: {ok}/{len(stages)} sent.")
            )
