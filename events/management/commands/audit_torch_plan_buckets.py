"""Where Torch recaps land on field-marketing plans — READ-ONLY by default.

For every Torch recap filed since plans launched (Event / Guerilla / Seeding,
plus Retail recaps that sit on a plan or ran on a plan's booked request):
the plan it counts toward (or none), the metrics it contributes there vs the
recap's own values, and flags:

  WRONG_TACTIC     recap type doesn't match the plan's tactic
  OTHER_PLAN       on a plan other than the one that booked its request
  MARKET_MISMATCH  plan market differs from the recap's market (judgment call)
  DATE_GAP         recap date more than 7 days outside the plan's days
  DOUBLE_COUNT     legacy + custom recap for the same BA shift both on plans
  MISSING          approved recap on a plan-booked request with no plan link
  THIRD_PARTY      3rd-party (exclude_from_aggregates) recap counted in actuals
  UNITS            cases on a non-seeding plan, or samples pushed off a seeding recap
  DELETED_PLAN     linked to a soft-deleted plan
  NOT_COUNTED      linked but unapproved / archived (kept out of totals; info)

Plan check: each plan's Actual rows (``build_plan_results``) vs an
independent sum of the contributed values of its countable recaps, and the
board's Logged value (manual log wins, else recaps).

``--attach-missing --apply`` links MISSING recaps exactly as approval does
(``auto_attach_on_approval``) when the booked plan's tactic matches the
recap type. Links only; a recap's own numbers are never touched.

    python manage.py audit_torch_plan_buckets [--since YYYY-MM-DD] [--attach-missing] [--apply]
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Min, Q
from django.utils import timezone

from events.demo_cancel import request_display_code
from events.field_marketing import _recap_logged, is_torch_tenant
from events.models import FieldMarketingEvent
from events.plan_actuals import RECAP_RELATED, Total, counts_toward_results, recap_day
from events.plan_results import (
    EXECUTION_LABELS,
    MARKET_STATES,
    SUGGEST_WINDOW_DAYS,
    TACTICS_FOR_EXECUTION,
    _ba_name,
    _day_gap,
    _recap_market,
    auto_attach_on_approval,
    build_plan_results,
)
from events.torch_retail_routing import (
    EXECUTION_RETAIL,
    EXECUTION_SEEDING,
    torch_recap_execution_type,
)
from recaps import plan_metrics as pm
from recaps.models import CustomRecap, PlanRecapLink, Recap
from tenants.models import Tenant

RECAP_COLUMNS = (
    "kind",
    "recap_id",
    "date",
    "type",
    "template",
    "ba",
    "source",
    "approved",
    "archived",
    "third_party",
    "event_id",
    "request",
    "recap_market",
    "recap_state",
    "booked_plans",
    "plan_id",
    "plan_name",
    "plan_tactic",
    "plan_market",
    "plan_starts_on",
    "link",
    "counted",
    "contributed",
    "recap_values",
    "flags",
)
PLAN_COLUMNS = (
    "plan_id",
    "plan_name",
    "tactic",
    "sampling_format",
    "market",
    "starts_on",
    "days",
    "deleted",
    "request",
    "linked",
    "counted",
    "actual_rows",
    "expected_from_recaps",
    "logged_manual",
    "logged_from_recaps",
    "flags",
)
_SUMMED_KEYS = tuple(k for k in pm.METRIC_ORDER if k != pm.SAMPLE_FORMAT)


def _should_count(recap) -> bool:
    return counts_toward_results(recap) and not getattr(recap, "exclude_from_aggregates", False)


def _source(recap, kind: str) -> str:
    if kind == pm.KIND_LEGACY:
        return "BA app (legacy)"
    if getattr(recap, "is_third_party", False):
        return "3rd-party agency link"
    if (getattr(recap, "source_label", "") or "").strip():
        return f"team link ({recap.source_label.strip()})"
    return "BA walk-up / app"


def _fmt_metric(metric: pm.RecapMetric) -> str:
    value = metric.text if metric.value is None else f"{metric.value:g}"
    if metric.parts:
        value += " (" + ", ".join(f"{name}:{qty}" for name, qty in metric.parts) + ")"
    return f"{metric.key}={value}"


def _fmt_metrics(metrics) -> str:
    return "; ".join(_fmt_metric(m) for m in metrics)


def _fmt_totals(totals: dict[str, float]) -> str:
    return "; ".join(f"{k}={totals[k]:g}" for k in _SUMMED_KEYS if k in totals)


def _plan_market_matches(plan, market_key: str | None, state: str | None) -> bool | None:
    if market_key:
        return plan.market == market_key
    if state:
        return MARKET_STATES.get(plan.market) == state
    return None


class Command(BaseCommand):
    help = "Read-only: where Torch recaps land on field-marketing plan buckets."

    def add_arguments(self, parser):
        parser.add_argument("--since", default="", help="YYYY-MM-DD (default: first plan)")
        parser.add_argument(
            "--attach-missing",
            action="store_true",
            help="Link MISSING recaps to their booked plan when the tactic matches.",
        )
        parser.add_argument("--apply", action="store_true", help="Write (default dry-run).")

    def handle(self, *args, **opts):
        tenants = [t for t in Tenant.objects.all().order_by("id") if is_torch_tenant(t)]
        if not tenants:
            raise CommandError("No Torch tenant found.")
        self.apply = bool(opts["apply"])
        self.attach = bool(opts["attach_missing"])
        mode = "APPLY (writing links)" if self.apply and self.attach else "DRY-RUN (no writes)"
        w = self.stdout.write
        for tenant in tenants:
            w(
                f"audit_torch_plan_buckets · {mode} · tenant id={tenant.id} "
                f"slug={tenant.slug!r} request_url_name={tenant.request_url_name!r}"
            )
            self._audit(tenant, opts["since"])

    # -- loading ------------------------------------------------------------

    def _since(self, tenant, raw: str) -> date | None:
        if raw:
            try:
                return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("--since must be YYYY-MM-DD") from None
        agg = FieldMarketingEvent.objects.filter(tenant=tenant).aggregate(
            created=Min("created_at"), starts=Min("starts_on")
        )
        if agg["created"] is None:
            return None
        first = min(timezone.localtime(agg["created"]).date(), agg["starts"])
        return first - timedelta(days=SUGGEST_WINDOW_DAYS)

    def _recaps(self, tenant, since: date):
        start = timezone.make_aware(datetime.combine(since, datetime.min.time()))
        window = Q(event__date__gte=start) | Q(event__start_time__gte=start) | Q(
            approved_at__gte=start
        )
        linked = Q(plan_link__plan__tenant=tenant)
        out = []
        custom = (
            CustomRecap.objects.filter(Q(event__tenant=tenant) | Q(tenant=tenant))
            .filter(window | linked)
            .select_related(*RECAP_RELATED, "custom_recap_template", "custom_recap_template__event_type")
            .distinct()
            .order_by("id")
        )
        out.extend((r, pm.KIND_CUSTOM) for r in custom)
        legacy = (
            Recap.objects.filter(event__tenant=tenant)
            .filter(window | linked)
            .select_related(*RECAP_RELATED)
            .distinct()
            .order_by("id")
        )
        out.extend((r, pm.KIND_LEGACY) for r in legacy)
        return out

    # -- audit --------------------------------------------------------------

    def _audit(self, tenant, since_raw: str) -> None:
        w = self.stdout.write
        since = self._since(tenant, since_raw)
        if since is None:
            w("  No plans for this tenant — nothing to audit.")
            return
        plans = {
            p.id: p
            for p in FieldMarketingEvent.objects.filter(tenant=tenant).select_related("request")
        }
        plans_by_request: dict[int, list] = defaultdict(list)
        for plan in plans.values():
            if plan.request_id and plan.deleted_at is None:
                plans_by_request[plan.request_id].append(plan)
        links = {
            (pm.KIND_CUSTOM, link.custom_recap_id)
            if link.custom_recap_id
            else (pm.KIND_LEGACY, link.recap_id): link
            for link in PlanRecapLink.objects.filter(plan__tenant=tenant)
        }
        w(f"  since={since.isoformat()} plans={len(plans)} links={len(links)}")

        recaps = self._recaps(tenant, since)
        rows: list[dict] = []
        type_counts: Counter = Counter()
        landed: Counter = Counter()
        flag_counts: Counter = Counter()
        skipped_retail = 0
        shift_links: dict[tuple, list] = defaultdict(list)
        missing: list[tuple] = []

        for recap, kind in recaps:
            execution = torch_recap_execution_type(recap)
            link = links.get((kind, recap.id))
            request_id = getattr(recap.event, "request_id", None)
            booked = plans_by_request.get(request_id, []) if request_id else []
            if execution == EXECUTION_RETAIL and link is None and not booked:
                skipped_retail += 1
                continue
            if not recap.approved and link is None:
                type_counts[f"{EXECUTION_LABELS[execution]} (unapproved, not on a plan)"] += 1
                continue

            label = EXECUTION_LABELS.get(execution, execution)
            type_counts[label] += 1
            own = pm.recap_metrics(recap, kind)
            market_key, state = _recap_market(recap)
            plan = plans.get(link.plan_id) if link else None
            contributed = (
                [m for m in own if m.key in set(link.metric_keys or [])] if link else []
            )
            flags: list[str] = []
            if link is not None:
                if recap.event_id and recap.ambassador_id:
                    shift_links[(recap.event_id, recap.ambassador_id)].append((kind, recap.id))
                if plan.activity not in TACTICS_FOR_EXECUTION.get(execution, frozenset()):
                    flags.append("WRONG_TACTIC")
                if booked and plan.id not in {p.id for p in booked}:
                    flags.append("OTHER_PLAN")
                if _plan_market_matches(plan, market_key, state) is False:
                    flags.append("MARKET_MISMATCH")
                if _day_gap(plan, recap_day(recap)) > SUGGEST_WINDOW_DAYS:
                    flags.append("DATE_GAP")
                keys = set(link.metric_keys or [])
                if pm.CASES_DROPPED in keys and plan.activity != FieldMarketingEvent.ACTIVITY_PRODUCT_SEEDING:
                    flags.append("UNITS")
                elif execution == EXECUTION_SEEDING and pm.SAMPLES_BY_SKU in keys:
                    flags.append("UNITS")
                if plan.deleted_at is not None:
                    flags.append("DELETED_PLAN")
                if getattr(recap, "exclude_from_aggregates", False):
                    flags.append("THIRD_PARTY")
                elif not counts_toward_results(recap):
                    flags.append("NOT_COUNTED")
            elif booked and _should_count(recap):
                flags.append("MISSING")
                missing.append((recap, kind, execution, booked))

            if link is not None and not (set(flags) - {"NOT_COUNTED"}) and _should_count(recap):
                landed[label] += 1
            for flag in flags:
                flag_counts[flag] += 1
            rows.append(
                {
                    "kind": kind,
                    "recap_id": recap.id,
                    "date": recap_day(recap).isoformat(),
                    "type": label,
                    "template": getattr(getattr(recap, "custom_recap_template", None), "name", "")
                    or getattr(getattr(recap.event, "custom_recap_template", None), "name", "")
                    or "",
                    "ba": _ba_name(recap),
                    "source": _source(recap, kind),
                    "approved": "yes" if recap.approved else "no",
                    "archived": "yes" if recap.archived_at else "no",
                    "third_party": "yes" if getattr(recap, "exclude_from_aggregates", False) else "no",
                    "event_id": recap.event_id,
                    "request": request_display_code(request_id) if request_id else "",
                    "recap_market": market_key or "",
                    "recap_state": state or "",
                    "booked_plans": " | ".join(f"{p.name} [{p.activity}]" for p in booked),
                    "plan_id": str(plan.uuid) if plan else "",
                    "plan_name": plan.name if plan else "",
                    "plan_tactic": plan.activity if plan else "",
                    "plan_market": plan.market if plan else "",
                    "plan_starts_on": plan.starts_on.isoformat() if plan else "",
                    "link": ("auto" if link.auto else "manual") if link else "",
                    "counted": "yes" if link and counts_toward_results(recap) else "no",
                    "contributed": _fmt_metrics(contributed),
                    "recap_values": _fmt_metrics(own),
                    "flags": " ".join(flags),
                }
            )

        for shift, entries in shift_links.items():
            if len({kind for kind, _ in entries}) > 1:
                ids = {rid for _, rid in entries}
                for row in rows:
                    if (row["kind"], row["recap_id"]) in entries:
                        row["flags"] = (row["flags"] + " DOUBLE_COUNT").strip()
                        flag_counts["DOUBLE_COUNT"] += 1
                w(f"  DOUBLE_COUNT shift event={shift[0]} ba={shift[1]} recaps={sorted(ids)}")

        w("")
        w("== Recaps checked by type ==")
        for label, n in sorted(type_counts.items()):
            w(f"  {label}: {n} checked, {landed.get(label, 0)} landed cleanly on a plan")
        w(f"  Retail recaps not on a plan and not on a plan-booked request (skipped): {skipped_retail}")
        w("== Flags ==")
        for flag, n in sorted(flag_counts.items()):
            w(f"  {flag}: {n}")
        if not flag_counts:
            w("  none")

        self._attach_missing(missing)
        plan_rows = self._plans(plans)

        w("")
        w("----- RECAPS CSV BEGIN -----")
        w(self._csv(RECAP_COLUMNS, rows))
        w("----- RECAPS CSV END -----")
        w("----- PLANS CSV BEGIN -----")
        w(self._csv(PLAN_COLUMNS, plan_rows))
        w("----- PLANS CSV END -----")

    def _attach_missing(self, missing) -> None:
        w = self.stdout.write
        w("== Missing links ==")
        if not missing:
            w("  none")
            return
        for recap, kind, execution, booked in missing:
            fits = [p for p in booked if p.activity in TACTICS_FOR_EXECUTION.get(execution, frozenset())]
            what = f"{kind} recap {recap.id} ({EXECUTION_LABELS[execution]}, {recap_day(recap)})"
            if not fits:
                w(f"  LEAVE {what}: booked plan tactic {[p.activity for p in booked]} ≠ recap type")
                continue
            if not (self.attach and self.apply):
                w(f"  WOULD ATTACH {what} → {fits[0].name}")
                continue
            with transaction.atomic():
                link = auto_attach_on_approval(kind=kind, recap_id=recap.id, actor=None)
            w(f"  ATTACHED {what} → {link.plan.name if link else 'nothing (no metrics)'}")

    def _plans(self, plans) -> list[dict]:
        w = self.stdout.write
        w("== Plan actuals vs recaps ==")
        out = []
        mismatches = 0
        for plan in sorted(plans.values(), key=lambda p: (p.starts_on, p.id)):
            payload = build_plan_results(plan, is_admin=True)
            plan_links = list(
                PlanRecapLink.objects.filter(plan=plan).select_related(
                    *(f"recap__{p}" for p in RECAP_RELATED),
                    *(f"custom_recap__{p}" for p in RECAP_RELATED),
                    "custom_recap__custom_recap_template",
                    "custom_recap__custom_recap_template__event_type",
                )
            )
            expected: dict[str, float] = {}
            for link in plan_links:
                recap, kind = (
                    (link.custom_recap, pm.KIND_CUSTOM)
                    if link.custom_recap_id
                    else (link.recap, pm.KIND_LEGACY)
                )
                if not _should_count(recap):
                    continue
                keys = set(link.metric_keys or [])
                for metric in pm.recap_metrics(recap, kind):
                    if metric.key in keys and metric.value is not None:
                        expected[metric.key] = expected.get(metric.key, 0) + metric.value
            actual = {
                row.key: row.actual
                for row in payload["rows"]
                if row.key in _SUMMED_KEYS and row.actual is not None
            }
            flags = []
            if {k: round(v, 2) for k, v in actual.items()} != {
                k: round(v, 2) for k, v in expected.items()
            }:
                flags.append("ACTUAL_MISMATCH")
                mismatches += 1
            from_recaps = _recap_logged(
                plan, {key: Total(key, key, value=value) for key, value in expected.items()}
            )
            manual = {
                f: getattr(plan, f)
                for f in ("logged_full_cans", "logged_emails", "logged_cases")
                if getattr(plan, f) is not None
            }
            overridden = [f for f, v in manual.items() if f in from_recaps and from_recaps[f] != v]
            if overridden:
                flags.append("MANUAL_LOG_OVERRIDES_RECAPS")
            if plan_links or manual:
                w(
                    f"  {plan.starts_on} {plan.name!r} [{plan.activity}/{plan.market}] "
                    f"links={len(plan_links)} counted={payload['counted']} "
                    f"actual={{{_fmt_totals(actual)}}} expected={{{_fmt_totals(expected)}}} "
                    f"{' '.join(flags)}".rstrip()
                )
            out.append(
                {
                    "plan_id": str(plan.uuid),
                    "plan_name": plan.name,
                    "tactic": plan.activity,
                    "sampling_format": plan.sampling_format or "",
                    "market": plan.market,
                    "starts_on": plan.starts_on.isoformat(),
                    "days": plan.days,
                    "deleted": "yes" if plan.deleted_at else "no",
                    "request": request_display_code(plan.request_id) if plan.request_id else "",
                    "linked": len(plan_links),
                    "counted": payload["counted"],
                    "actual_rows": _fmt_totals(actual),
                    "expected_from_recaps": _fmt_totals(expected),
                    "logged_manual": "; ".join(f"{k}={v}" for k, v in manual.items()),
                    "logged_from_recaps": "; ".join(f"{k}={v}" for k, v in from_recaps.items()),
                    "flags": " ".join(flags),
                }
            )
        w(f"  plans with Actual ≠ sum of countable recaps: {mismatches}")
        return out

    @staticmethod
    def _csv(columns, rows) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().rstrip("\n")
