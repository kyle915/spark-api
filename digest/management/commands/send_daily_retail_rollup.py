"""Email the executive daily retail-sampling roll-up (yesterday's approved recaps).

For each tenant with an enabled :class:`digest.models.TenantDailyRollup` row and
recipients, builds :func:`recaps.daily_retail_rollup.build_daily_retail_rollup`
for the previous calendar day in that row's timezone (default Pacific — the
same clock the Monday weekly digest runs on) and sends
:class:`recaps.envelopes.DailyRetailRollupMailer`.

SAFE DEFAULT — OFF. No row, ``enabled=False`` or no recipients → nothing sent.
A day with zero approved canonical Retail recaps is skipped (no empty email).
Each live send stamps :class:`digest.models.TenantDailyRollupSend`
(unique per tenant + day) BEFORE mailing, so a re-run or a double-fired
schedule never sends the same day twice; a failed send removes its stamp.

Flags:
    --dry-run            Build + log, send nothing.
    --tenant <id|slug>   One tenant (exact id, slug, then request_url_name).
    --date YYYY-MM-DD    Report this approval day instead of yesterday.
    --preview-to <email> Send ONE preview to an @igniteproductions.co address,
                         even while the tenant is off. Never stamps the day.
    --html-file <path>   Also write the rendered HTML to this path.
    --not-before <hour>  Skip a tenant while its local time is before this hour.
    --not-after <hour>   Skip a tenant once its local time reaches this hour.
                         The schedule fires at 19:00, 20:00 and 21:00 UTC with
                         12 / 14, so the send lands at 12:00 PM Pacific in both
                         PDT and PST (the extra fire is a backup for a late or
                         dropped GitHub run; the per-day stamp dedupes).
"""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from ambassadors.checkin_web import _brand_primary_color
from digest.models import TenantDailyRollup, TenantDailyRollupSend
from recaps.daily_retail_rollup import (
    DEFAULT_ROLLUP_TIMEZONE,
    build_daily_retail_rollup,
    previous_day,
)
from recaps.envelopes import DailyRetailRollupMailer
from tenants.models import Tenant

logger = logging.getLogger(__name__)

PREVIEW_DOMAIN = "@igniteproductions.co"


def resolve_tenant(raw: str) -> Tenant:
    """Exact id, then exact slug, then exact request_url_name."""
    value = (raw or "").strip()
    if value.isdigit():
        tenant = Tenant.objects.filter(id=int(value)).first()
    else:
        tenant = (
            Tenant.objects.filter(slug__iexact=value).first()
            or Tenant.objects.filter(request_url_name__iexact=value).first()
        )
    if tenant is None:
        raise CommandError(f"No tenant matches {raw!r} (id, slug, or request_url_name).")
    return tenant


class Command(BaseCommand):
    help = "Send the executive daily retail roll-up to opted-in tenants (OFF by default)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--tenant", default="")
        parser.add_argument("--date", default="")
        parser.add_argument("--preview-to", default="")
        parser.add_argument("--html-file", default="")
        parser.add_argument("--not-before", type=int, default=None)
        parser.add_argument("--not-after", type=int, default=None)

    def handle(self, *args, **opts):
        self.not_before = opts.get("not_before")
        self.not_after = opts.get("not_after")
        dry_run = bool(opts["dry_run"])
        preview_to = (opts.get("preview_to") or "").strip().lower()
        report_date = None
        if opts.get("date"):
            try:
                report_date = datetime.date.fromisoformat(opts["date"].strip())
            except ValueError as exc:
                raise CommandError(f"--date must be YYYY-MM-DD: {opts['date']}") from exc
        if preview_to and not preview_to.endswith(PREVIEW_DOMAIN):
            raise CommandError(f"--preview-to must be an {PREVIEW_DOMAIN} address.")
        if preview_to and not opts.get("tenant"):
            raise CommandError("--preview-to requires --tenant.")

        if opts.get("tenant"):
            tenants = [resolve_tenant(opts["tenant"])]
        else:
            tenants = list(
                Tenant.active().filter(daily_rollup__enabled=True).order_by("id")
            )

        now = timezone.now()
        mode = "DRY-RUN" if dry_run else ("PREVIEW" if preview_to else "live")
        self.stdout.write(f"Daily retail roll-up — {now:%Y-%m-%d %H:%M} UTC, mode {mode}.")
        sent = skipped = failed = 0
        for tenant in tenants:
            try:
                if self._run_tenant(
                    tenant, now, report_date, preview_to, dry_run, opts.get("html_file") or ""
                ):
                    sent += 1
                else:
                    skipped += 1
            except Exception:
                failed += 1
                logger.exception("Daily retail roll-up failed for tenant=%s", tenant.id)
                self.stdout.write(f"  - {tenant.name} (id={tenant.id}): FAILED — see logs.")
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. tenants={len(tenants)} sent={sent} skipped={skipped} failed={failed}."
            )
        )

    def _run_tenant(self, tenant, now, report_date, preview_to, dry_run, html_file) -> bool:
        label = f"{tenant.name} (id={tenant.id})"
        config = TenantDailyRollup.objects.filter(tenant=tenant).first()
        tz_name = (config.timezone if config else "") or DEFAULT_ROLLUP_TIMEZONE
        local_hour = now.astimezone(ZoneInfo(tz_name)).hour
        if self.not_before is not None and local_hour < self.not_before:
            self.stdout.write(
                f"  - {label}: SKIP — {local_hour}:00 local is before {self.not_before}:00."
            )
            return False
        if self.not_after is not None and local_hour >= self.not_after:
            self.stdout.write(
                f"  - {label}: SKIP — {local_hour}:00 local is past the send window "
                f"(before {self.not_after}:00)."
            )
            return False
        day = report_date or previous_day(now, tz_name)

        if preview_to:
            recipients = [preview_to]
        else:
            if config is None or not config.enabled:
                self.stdout.write(f"  - {label}: SKIP — daily roll-up is off.")
                return False
            recipients = [e for e in (config.recipients or []) if (e or "").strip()]
            if not recipients:
                self.stdout.write(f"  - {label}: SKIP — no recipients.")
                return False
            if TenantDailyRollupSend.objects.filter(tenant=tenant, report_date=day).exists():
                self.stdout.write(f"  - {label}: SKIP — {day} already sent.")
                return False

        rollup = build_daily_retail_rollup(tenant.id, day, tz_name)
        d = rollup.day
        summary = (
            f"{day} ({tz_name}): {len(rollup.rows)} approved Retail recap(s), "
            f"{len(rollup.third_party_rows)} 3rd party; demos={d['demos']} "
            f"sampled={d['consumers_sampled']} units={d['units_sold']} "
            f"conv={d['conversion_pct']}"
        )
        if not rollup.has_content:
            logger.info(
                "Daily retail roll-up: tenant=%s %s skipped, nothing approved.", tenant.id, day
            )
            self.stdout.write(f"  - {label}: SKIP — {summary}; no email for an empty day.")
            return False

        mailer = DailyRetailRollupMailer(
            recipients=recipients,
            tenant_name=tenant.name or "",
            rollup=rollup,
            brand_color=_brand_primary_color(tenant),
            preview=bool(preview_to),
        )
        if html_file:
            with open(html_file, "w", encoding="utf-8") as fh:
                fh.write(mailer.envelope().render_template())
        if dry_run:
            self.stdout.write(
                f"  - {label}: would email {recipients} — {summary} — NOT sent (dry-run)."
            )
            return False

        stamp = None
        if not preview_to:
            try:
                with transaction.atomic():
                    stamp = TenantDailyRollupSend.objects.create(
                        tenant=tenant,
                        report_date=day,
                        recipients=recipients,
                        recap_count=len(rollup.rows),
                    )
            except IntegrityError:
                self.stdout.write(f"  - {label}: SKIP — {day} already sent.")
                return False
        try:
            mailer.send_now()
        except Exception:
            if stamp is not None:
                stamp.delete()
            raise
        self.stdout.write(f"  - {label}: sent to {recipients} — {summary}.")
        return True
