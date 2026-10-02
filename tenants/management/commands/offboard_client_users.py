"""Offboard people who left a client brand (e.g. Liquid Death layoffs, Oct 2026).

For each email, within the tenant:
  * deactivates the user (blocks password, magic-link and SSO sign-in) and
    their membership on the tenant (drops them from client recap mail);
  * removes them from Tenant.recap_recipient_emails;
  * clears Tenant.default_external_rmm if it points at them;
  * reassigns their upcoming (today onward) requests and events to
    ``--reassign-to``. Past ones stay theirs so reporting keeps crediting them.

SAFE: dry-run by default; ``--apply`` writes. Re-runnable.
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


def _without_emails(raw: str, drop: set[str]) -> str:
    kept = [
        token.strip()
        for token in re.split(r"[,\n;]+", raw or "")
        if token.strip() and token.strip().lower() not in drop
    ]
    return ", ".join(kept)


class Command(BaseCommand):
    help = "Deactivate departed client users and hand their upcoming work to someone else. Dry-run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", required=True)
        parser.add_argument("--emails", required=True, help="Comma-separated emails to offboard.")
        parser.add_argument("--reassign-to", default="", help="Email of the user who takes over upcoming work.")
        parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")

    def handle(self, *args, **opts):
        from events.models import Event, Request
        from tenants.models import Tenant, TenantedUser, User

        apply = opts["apply"]
        slug = opts["tenant_slug"].strip()
        tenant = (
            Tenant.objects.filter(slug=slug).order_by("id").first()
            or Tenant.objects.filter(request_url_name=slug).order_by("id").first()
        )
        if tenant is None:
            raise CommandError(f"tenant-not-found: {slug}")

        emails = {e.strip().lower() for e in opts["emails"].split(",") if e.strip()}
        if not emails:
            raise CommandError("--emails is empty")
        heir = None
        if opts["reassign_to"].strip():
            heir = User.objects.filter(email__iexact=opts["reassign_to"].strip()).first()
            if heir is None:
                raise CommandError(f"reassign-to user not found: {opts['reassign_to']}")
            if heir.email.lower() in emails:
                raise CommandError("--reassign-to is one of the people being offboarded")

        w = self.stdout.write
        w(f"{'APPLY' if apply else 'DRY-RUN'} offboard on {tenant.name} (id={tenant.id})")
        if heir is not None:
            heir_member = TenantedUser.objects.filter(user=heir, tenant=tenant, is_active=True).exists()
            w(f"  heir {heir.email}: active={heir.is_active} membership={heir_member}")
            if not heir.is_active or not heir_member:
                w("    ! heir is inactive or not a member of this tenant")
        start_of_today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)

        with transaction.atomic():
            users = list(
                User.objects.filter(
                    email__iregex=r"^(" + "|".join(re.escape(e) for e in emails) + r")$"
                ).order_by("id")
            )
            found = {u.email.lower() for u in users}
            for missing in sorted(emails - found):
                w(f"  {missing}: no Spark user")

            for user in users:
                memberships = TenantedUser.objects.filter(user=user, tenant=tenant, is_active=True)
                other_tenants = TenantedUser.objects.filter(user=user, is_active=True).exclude(tenant=tenant).count()
                upcoming_requests = Request.objects.filter(
                    tenant=tenant, rmm_asigned=user, deleted_at__isnull=True, date__gte=start_of_today
                )
                upcoming_events = Event.objects.filter(
                    tenant=tenant, rmm_asigned=user, date__gte=start_of_today
                )
                n_req, n_ev = upcoming_requests.count(), upcoming_events.count()
                role = getattr(getattr(user, "role", None), "slug", "") or "-"
                w(
                    f"  {user.email}: role={role} active={user.is_active} "
                    f"membership={memberships.exists()} other_tenants={other_tenants} "
                    f"upcoming_requests={n_req} upcoming_events={n_ev}"
                )
                if (n_req or n_ev) and heir is None:
                    w("    ! upcoming work left on a deactivated user (no --reassign-to)")
                if not apply:
                    continue
                memberships.update(is_active=False)
                if other_tenants == 0 and user.is_active:
                    user.is_active = False
                    user.save(update_fields=["is_active"])
                if heir is not None:
                    upcoming_requests.update(rmm_asigned=heir, updated_at=timezone.now())
                    upcoming_events.update(rmm_asigned=heir)

            recipients = tenant.recap_recipient_emails or ""
            trimmed = _without_emails(recipients, emails)
            if trimmed != ", ".join(t.strip() for t in re.split(r"[,\n;]+", recipients) if t.strip()):
                w(f"  recap_recipient_emails: {recipients!r} -> {trimmed!r}")
                if apply:
                    tenant.recap_recipient_emails = trimmed
                    tenant.save(update_fields=["recap_recipient_emails"])

            default_rmm = tenant.default_external_rmm
            if default_rmm is not None and (default_rmm.email or "").lower() in emails:
                w(f"  default_external_rmm: {default_rmm.email} -> cleared (territory routing resumes)")
                if apply:
                    tenant.default_external_rmm = None
                    tenant.save(update_fields=["default_external_rmm"])

        if heir is not None:
            w(f"  upcoming work reassigned to: {heir.email}" if apply else f"  would reassign upcoming work to: {heir.email}")
