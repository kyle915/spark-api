"""Strip TORCH_SUPPRESS_EMAILS from Torch tenant recipient text fields.

Baesha (and any future TORCH_SUPPRESS address) is filtered at send time, but
stored Tenant.recap_recipient_emails / request_approved_cc_emails can still
list her. This command removes those addresses from Torch rows only — it does
NOT deactivate the user or touch other brands.

Dry-run by default; pass --apply to write.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from events.torch_portal import TORCH_SUPPRESS_EMAILS, TORCH_TENANT_SLUGS, is_torch_tenant
from tenants.models import Tenant, parse_recipient_emails


def _without_suppressed(raw: str | None) -> str:
    drop = {e.lower() for e in TORCH_SUPPRESS_EMAILS}
    kept = [
        token
        for token in parse_recipient_emails(raw)
        if token.lower() not in drop
    ]
    return ", ".join(kept)


class Command(BaseCommand):
    help = (
        "Remove TORCH_SUPPRESS_EMAILS from Torch tenant recipient fields. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write changes (default: dry-run).",
        )

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        w = self.stdout.write
        w(f"{'APPLY' if apply else 'DRY-RUN'} strip Torch suppress recipients")
        w(f"  suppress={sorted(TORCH_SUPPRESS_EMAILS)}")

        # Prefer exact slug / request_url_name matches, then is_torch_tenant
        # for name-only rows (legacy "Torch" / "Torch THC").
        candidates = list(
            Tenant.objects.filter(
                slug__in=TORCH_TENANT_SLUGS
            ).order_by("id")
        ) + list(
            Tenant.objects.filter(
                request_url_name__in=TORCH_TENANT_SLUGS
            ).order_by("id")
        )
        seen: set[int] = set()
        tenants: list[Tenant] = []
        for tenant in candidates:
            if tenant.id in seen:
                continue
            seen.add(tenant.id)
            tenants.append(tenant)
        for tenant in Tenant.objects.exclude(id__in=seen).order_by("id"):
            if is_torch_tenant(tenant):
                tenants.append(tenant)

        if not tenants:
            w("  no Torch tenants found")
            return

        fields = ("recap_recipient_emails", "request_approved_cc_emails")
        changed = 0
        for tenant in tenants:
            w(
                f"  tenant id={tenant.id} slug={tenant.slug!r} "
                f"request_url_name={tenant.request_url_name!r} name={tenant.name!r}"
            )
            updates: list[str] = []
            for field in fields:
                raw = getattr(tenant, field, None) or ""
                trimmed = _without_suppressed(raw)
                before = ", ".join(parse_recipient_emails(raw))
                if trimmed == before:
                    continue
                w(f"    {field}: {raw!r} -> {trimmed!r}")
                setattr(tenant, field, trimmed)
                updates.append(field)
            if not updates:
                w("    (no suppressed addresses in recipient fields)")
                continue
            changed += 1
            if apply:
                tenant.save(update_fields=[*updates])

        w(f"  tenants with changes: {changed}")
        if not apply and changed:
            w("  re-run with --apply to write")
