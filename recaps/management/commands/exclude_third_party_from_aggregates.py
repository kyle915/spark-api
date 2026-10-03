"""Keep a tenant's 3rd-party (agency link) recaps out of every aggregate.

Turns on ``Tenant.checkin_recap_excludes_aggregates`` (new filings through
``checkin_recap_code`` get stamped on submit) and backfills
``CustomRecap.exclude_from_aggregates`` on the tenant's existing
``is_third_party`` recaps. The recaps stay listed, approvable, and
shareable with their own numbers; only rollups skip them. Never touches a
recap metric, the check-in codes, or approval.

Also REPORTS (never writes) other 3rd-party signals — agency stub identity
(no phone given on the agency link) or a typed store on a recap not marked
3rd party — so a mis-stamped filing is visible for review.

DRY-RUN unless ``--apply``. Logs before/after per recap.

    python manage.py exclude_third_party_from_aggregates --tenant torch-thc
    python manage.py exclude_third_party_from_aggregates --tenant torch-thc --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DEFAULT_TENANT = "torch-thc"
# recap_only_identity_phone() mints "000" + 10 digits for agency filers who
# leave phone blank; the walk-up stub email is checkin-<digits>@walkup.spark.
AGENCY_STUB_EMAIL_PREFIX = "checkin-000"


class Command(BaseCommand):
    help = "Exclude a tenant's 3rd-party (agency link) recaps from aggregates (dry-run default)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=DEFAULT_TENANT)
        parser.add_argument("--apply", action="store_true")

    @staticmethod
    def _resolve_tenant(ident: str):
        from tenants.models import Tenant

        ident = (ident or DEFAULT_TENANT).strip()
        if ident.isdigit():
            tenant = Tenant.objects.filter(id=int(ident)).first()
        else:
            tenant = (
                Tenant.objects.filter(slug=ident).order_by("id").first()
                or Tenant.objects.filter(request_url_name=ident).order_by("id").first()
            )
        if tenant is None:
            raise CommandError(f"tenant-not-found: {ident}")
        return tenant

    def _line(self, r) -> str:
        from recaps.types import _consumers_sampled_from_fields, _sold_units_from_fields

        pairs = [(v.custom_field.name, v.value) for v in r.custom_field_value.all()]
        ev = r.event
        when = (ev.date or ev.start_time) if ev else None
        amb = r.ambassador
        ba = ""
        if amb is not None and amb.user is not None:
            ba = f"{amb.user.first_name or ''} {amb.user.last_name or ''}".strip()
        status = "archived" if r.archived_at else ("approved" if r.approved else "needs_review")
        store = r.typed_store_name or r.name or (ev.name if ev else "")
        return (
            f"#{r.id} {when.date() if when else '-'} [{status}] store={store!r} ba={ba or r.external_ba_name or ''!r} "
            f"sampled={_consumers_sampled_from_fields(pairs)} units={_sold_units_from_fields(pairs)} "
            f"event={r.event_id}"
        )

    def handle(self, *args, **opts):
        from recaps.models import CustomRecap

        apply = bool(opts.get("apply"))
        tenant = self._resolve_tenant(opts.get("tenant"))
        w = self.stdout.write
        code = (tenant.checkin_recap_code or "").strip()
        w(f"{'APPLY' if apply else 'DRY RUN'} — tenant [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}")
        w(f"3rd-party link: /checkin/{code or '(none)'}  checkin_code: /checkin/{tenant.checkin_code or '(none)'} (neither changed)")
        if not code:
            raise CommandError("tenant has no checkin_recap_code (no 3rd-party link)")

        base = (
            CustomRecap.objects.filter(tenant=tenant)
            .select_related("event", "ambassador__user")
            .prefetch_related("custom_field_value__custom_field")
            .order_by("id")
        )
        flagged = list(base.filter(is_third_party=True))
        w(f"\nTenant.checkin_recap_excludes_aggregates: {tenant.checkin_recap_excludes_aggregates} -> True")
        w(f"\n3rd-party recaps (is_third_party, filed via /checkin/{code}): {len(flagged)}")
        to_set = []
        for r in flagged:
            before = r.exclude_from_aggregates
            w(f"  {self._line(r)}  exclude_from_aggregates: {before} -> True{'' if not before else ' (already)'}")
            if not before:
                to_set.append(r.id)

        stub = list(
            base.filter(is_third_party=False, ambassador__user__email__istartswith=AGENCY_STUB_EMAIL_PREFIX)
        )
        typed = list(base.filter(is_third_party=False).exclude(typed_store_name=""))
        w("\nOther 3rd-party signals on recaps NOT marked 3rd party (report only, not changed):")
        w(f"  agency stub identity (no phone on agency link): {len(stub)}")
        for r in stub:
            w(f"    {self._line(r)}")
        w(f"  typed agency store name set: {len(typed)}")
        for r in typed:
            w(f"    {self._line(r)}")

        if not apply:
            w(f"\nDRY RUN — would set exclude_from_aggregates on {len(to_set)} recap(s) and turn the tenant flag on.")
            return
        with transaction.atomic():
            if not tenant.checkin_recap_excludes_aggregates:
                tenant.checkin_recap_excludes_aggregates = True
                tenant.save(update_fields=["checkin_recap_excludes_aggregates"])
            n = CustomRecap.objects.filter(id__in=to_set, tenant=tenant, is_third_party=True).update(
                exclude_from_aggregates=True
            )
        w(f"\nAPPLIED — exclude_from_aggregates set on {n} recap(s); tenant flag on.")
