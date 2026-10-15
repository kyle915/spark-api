"""Mark a Torch field-marketing plan as a setup / load-in day — DRY-RUN by default.

A setup / load-in plan (e.g. the day before a sponsorship, loading in the
booth) is work for another event, so it never counts toward "Local event
sponsorships" days, planned or actual. Its recaps still list under Event
recaps and still count toward cans / pours / emails.

    python manage.py set_plan_setup_day --search "RARE"
    python manage.py set_plan_setup_day --plan REQ-ABCD [--off] [--apply]

``--search`` lists live Torch plans whose name / address / notes contain the
text (any month), with their id and request code — nothing is written.
``--plan`` takes a plan uuid or its booked request code (REQ-XXXX) and must
match exactly one live event plan. The scorecard for that plan's month is
printed per market before and after (a dry run rolls the change back).
Sends nothing; never touches a recap.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from events.demo_cancel import request_display_code
from events.field_marketing import (
    _SPONSORSHIP_ACTIVITIES,
    ACTIVITIES,
    MARKETS,
    build_board,
)
from events.models import FieldMarketingEvent
from events.plan_actuals import result_counts
from tenants.models import Tenant

TENANT_SLUG = "torch-thc"
TENANT_FORM_SLUG = "keee-torch-thc"
_KPIS = ("sponsorship_days", "full_cans", "pour_samples", "emails")


class _DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Mark a Torch plan as a setup / load-in day (dry-run by default; --apply writes)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=TENANT_SLUG)
        parser.add_argument("--search", default="")
        parser.add_argument("--plan", default="", help="plan uuid or REQ-XXXX")
        parser.add_argument("--off", action="store_true", help="clear the flag instead")
        parser.add_argument("--apply", action="store_true")

    def _tenant(self, needle: str) -> Tenant:
        needle = (needle or TENANT_SLUG).strip()
        for lookup in ("slug__iexact", "request_url_name__iexact"):
            rows = list(Tenant.objects.filter(**{lookup: needle}).order_by("id"))
            if len(rows) == 1:
                return rows[0]
            if len(rows) > 1:
                raise CommandError(f"{len(rows)} tenants match {needle!r}.")
        if needle == TENANT_SLUG:
            return self._tenant(TENANT_FORM_SLUG)
        raise CommandError(f"tenant-not-found: {needle}")

    def _line(self, plan: FieldMarketingEvent, counts) -> str:
        code = request_display_code(plan.request_id) if plan.request_id else "no request"
        counted, pending = counts.get(plan.id, (0, 0))
        return (
            f"  {plan.uuid}  {code:<10} {plan.starts_on}  {plan.market:<13} "
            f"{ACTIVITIES.get(plan.activity, plan.activity)} · {plan.days} day(s)"
            f"{' · SETUP DAY' if plan.setup_day else ''} · recaps {counted} counted"
            f"{f' / {pending} in review' if pending else ''}\n"
            f"      {plan.name!r}{f' — {plan.address}' if plan.address else ''}"
        )

    def _find(self, tenant, needle: str) -> FieldMarketingEvent:
        live = FieldMarketingEvent.objects.filter(tenant=tenant, deleted_at__isnull=True)
        raw = needle.strip()
        rows = []
        if raw.upper().startswith("REQ-"):
            rows = [p for p in live.exclude(request_id=None) if request_display_code(p.request_id) == raw.upper()]
        else:
            try:
                rows = list(live.filter(uuid=raw))
            except Exception:  # noqa: BLE001 — not a uuid
                rows = []
        if len(rows) != 1:
            raise CommandError(f"{len(rows)} live Torch plans match {raw!r} — need exactly one.")
        return rows[0]

    def _scorecard(self, tenant, month: str, label: str) -> None:
        self.stdout.write(f"\nScorecard {month} · {label}:")
        for key, market_label, *_rest in (("", "All markets"), *MARKETS):
            board = build_board(tenant, month=month, market=key or None)
            cells = []
            for kpi in board["kpis"]:
                if kpi["key"] not in _KPIS:
                    continue
                planned = "—" if kpi["planned"] is None else f"{kpi['planned']:,}"
                logged = "—" if kpi["logged"] is None else f"{kpi['logged']:,}"
                cells.append(f"{kpi['key']} actual {logged} / planned {planned}")
            self.stdout.write(f"  {market_label:<16} " + " · ".join(cells))

    def handle(self, *args, **opts):
        tenant = self._tenant(opts["tenant"])
        apply = bool(opts["apply"])
        self.stdout.write(f"Tenant : [{tenant.id}] {tenant.name!r}")
        self.stdout.write(f"Mode   : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")

        search = (opts["search"] or "").strip()
        if search:
            plans = list(
                FieldMarketingEvent.objects.filter(tenant=tenant, deleted_at__isnull=True)
                .filter(
                    Q(name__icontains=search)
                    | Q(address__icontains=search)
                    | Q(notes__icontains=search)
                )
                .order_by("starts_on", "id")
            )
            counts = result_counts([p.id for p in plans])
            self.stdout.write(f"\nPlans matching {search!r}: {len(plans)}")
            for plan in plans:
                self.stdout.write(self._line(plan, counts))

        if not opts["plan"]:
            if not search:
                raise CommandError("Pass --search TEXT to list plans or --plan ID to flag one.")
            return

        plan = self._find(tenant, opts["plan"])
        want = not opts["off"]
        if want and plan.activity not in _SPONSORSHIP_ACTIVITIES:
            raise CommandError("Only event plans count event days; this plan has none to drop.")
        month = plan.starts_on.strftime("%Y-%m")
        self.stdout.write("\nPlan:")
        self.stdout.write(self._line(plan, result_counts([plan.id])))
        self._scorecard(tenant, month, "before")
        if plan.setup_day == want:
            self.stdout.write(f"\nAlready setup_day={want} — nothing to change.")
            return
        try:
            with transaction.atomic():
                plan.setup_day = want
                plan.save(update_fields=["setup_day", "updated_at"])
                self._scorecard(tenant, month, "after")
                if not apply:
                    raise _DryRun
        except _DryRun:
            self.stdout.write(self.style.WARNING("\nDRY-RUN — rolled back. Re-run with --apply."))
            return
        self.stdout.write(self.style.SUCCESS(f"\nAPPLIED — setup_day={want} on {plan.uuid}."))
