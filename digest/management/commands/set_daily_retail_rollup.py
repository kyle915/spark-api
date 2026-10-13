"""Turn a tenant's executive daily retail roll-up on/off and set its recipients.

DRY-RUN by default — prints the before/after; only ``--apply`` writes.

Recipients: ``--recipients "a@x.com,b@y.com"`` replaces the list; blank keeps
the current list, or (first setup) seeds Torch's default executive list from
:data:`events.torch_retail_routing.TORCH_DAILY_RETAIL_ROLLUP`.

Usage:
    python manage.py set_daily_retail_rollup --tenant keee-torch-thc --enabled false --apply
    python manage.py set_daily_retail_rollup --tenant keee-torch-thc --enabled true --apply
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.management.base import BaseCommand, CommandError

from digest.management.commands.send_daily_retail_rollup import resolve_tenant
from digest.models import TenantDailyRollup
from events.torch_portal import is_torch_tenant
from events.torch_retail_routing import torch_daily_retail_rollup_emails
from recaps.daily_retail_rollup import DEFAULT_ROLLUP_TIMEZONE


def _parse_bool(raw: str) -> bool | None:
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise CommandError(f"--enabled must be true or false, got {raw!r}")


def _parse_recipients(raw: str) -> list[str] | None:
    parts = [p.strip() for p in (raw or "").replace(";", ",").split(",") if p.strip()]
    if not parts:
        return None
    bad = [p for p in parts if "@" not in p]
    if bad:
        raise CommandError(f"Not email addresses: {bad}")
    out: list[str] = []
    for part in parts:
        if part.lower() not in {e.lower() for e in out}:
            out.append(part)
    return out


class Command(BaseCommand):
    help = "Set a tenant's daily retail roll-up switch + recipients (dry-run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True)
        parser.add_argument("--enabled", default="")
        parser.add_argument("--recipients", default="")
        parser.add_argument("--timezone", default="")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        tenant = resolve_tenant(opts["tenant"])
        enabled = _parse_bool(opts.get("enabled") or "")
        recipients = _parse_recipients(opts.get("recipients") or "")
        tz_name = (opts.get("timezone") or "").strip()
        if tz_name:
            try:
                ZoneInfo(tz_name)
            except ZoneInfoNotFoundError as exc:
                raise CommandError(f"Unknown timezone {tz_name!r}") from exc

        config = TenantDailyRollup.objects.filter(tenant=tenant).first()
        before = (
            {"enabled": config.enabled, "recipients": config.recipients, "timezone": config.timezone}
            if config
            else None
        )
        current = before or {
            "enabled": False,
            "recipients": torch_daily_retail_rollup_emails() if is_torch_tenant(tenant) else [],
            "timezone": DEFAULT_ROLLUP_TIMEZONE,
        }
        after = {
            "enabled": current["enabled"] if enabled is None else enabled,
            "recipients": current["recipients"] if recipients is None else recipients,
            "timezone": tz_name or current["timezone"],
        }

        self.stdout.write(f"Tenant: {tenant.name} (id={tenant.id}, slug={tenant.slug})")
        self.stdout.write(f"  before: {before or '(no row — roll-up off)'}")
        self.stdout.write(f"  after : {after}")
        if after["enabled"] and not after["recipients"]:
            raise CommandError("Refusing to enable with no recipients.")
        if not opts["apply"]:
            self.stdout.write("DRY-RUN — nothing written. Re-run with --apply.")
            return
        TenantDailyRollup.objects.update_or_create(tenant=tenant, defaults=after)
        self.stdout.write(self.style.SUCCESS("Applied."))
