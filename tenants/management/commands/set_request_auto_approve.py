"""Turn per-tenant request auto-approve on (or off) and set its CC list.

With ``Tenant.auto_approve_requests`` on, a brand's public spark-form and
client-portal requests land approved (Event materialized like an Approvals
click) and the approval email goes to the requestor with
``Tenant.request_approved_cc_emails`` on CC. Recaps keep the admin-approval
gate either way.

Tenants resolve by exact slug, then exact request_url_name. ``--cc`` replaces
the stored list; omit it to leave the list alone. ``--off`` turns auto-approve
off (the CC list is kept so turning it back on is one flag).

Dry-run by default; ``--apply`` writes. Prod: ``/internal/cron/set-request-auto-approve``
or the "Set request auto-approve" GitHub Action.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tenants.models import Tenant, parse_recipient_emails


def resolve_tenant(key: str) -> Tenant:
    for lookup in ("slug__iexact", "request_url_name__iexact"):
        rows = list(Tenant.objects.filter(**{lookup: key}).order_by("id")[:2])
        if len(rows) > 1:
            raise CommandError(f"{lookup}={key!r} matched several tenants.")
        if rows:
            return rows[0]
    raise CommandError(f"No tenant with slug or request_url_name {key!r}.")


class Command(BaseCommand):
    help = (
        "Turn per-tenant request auto-approve on/off and set its CC list "
        "(dry-run by default; --apply to write)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenants",
            required=True,
            help="Comma-separated tenant slugs (exact slug, then exact request_url_name).",
        )
        parser.add_argument(
            "--cc",
            default=None,
            help="Comma-separated CC emails for the approval email (replaces the stored list).",
        )
        parser.add_argument("--off", action="store_true", help="Turn auto-approve OFF.")
        parser.add_argument("--apply", action="store_true", help="Actually write.")

    def handle(self, *args, **opts):
        keys = [k.strip() for k in (opts["tenants"] or "").split(",") if k.strip()]
        if not keys:
            raise CommandError("Pass --tenants.")
        tenants = [resolve_tenant(k) for k in keys]
        enabled = not opts["off"]
        cc_raw = opts.get("cc")
        cc = ", ".join(parse_recipient_emails(cc_raw)) if cc_raw is not None else None
        if cc_raw is not None and not cc and enabled:
            raise CommandError("--cc has no valid email addresses.")
        apply_ = bool(opts["apply"])

        self.stdout.write(f"Mode : {'APPLY (writing)' if apply_ else 'DRY-RUN (no writes)'}")
        for tenant in tenants:
            wanted = {"auto_approve_requests": enabled}
            if cc is not None:
                wanted["request_approved_cc_emails"] = cc
            changed = {k: v for k, v in wanted.items() if getattr(tenant, k) != v}
            self.stdout.write(f"\n[{tenant.id}] {tenant.name} (slug={tenant.slug})")
            for key in ("auto_approve_requests", "request_approved_cc_emails"):
                current = getattr(tenant, key)
                if key in changed:
                    self.stdout.write(f"  {key}: {current!r} -> {changed[key]!r}")
                else:
                    self.stdout.write(f"  {key}: {current!r} (unchanged)")
            if changed and apply_:
                Tenant.objects.filter(pk=tenant.pk).update(**changed)
                self.stdout.write(self.style.SUCCESS("  written"))

        if not apply_:
            self.stdout.write(self.style.WARNING("\nDRY-RUN — re-run with --apply to write."))
