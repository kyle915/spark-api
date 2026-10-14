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

Plan events: each live plan's booked event(s) with their program (event
type), BAs assigned and recaps filed. WRONG_PROGRAM = the event runs a
program that doesn't match the plan's tactic (e.g. an Event Activation plan
booked as Retail Sampling), so BAs get the wrong recap form and walk-ups
never join it.

``--attach-missing --apply`` links MISSING recaps exactly as approval does
(``auto_attach_on_approval``) when the booked plan's tactic matches the
recap type. ``--fix-event-types --apply`` sets WRONG_PROGRAM events with no
recap yet to the plan's program (events with recaps are listed, not
changed). ``--attach custom:1867=REQ-MJM5`` (repeatable, optional
``:key+key`` metric list, default = the tactic's defaults) links one approved
recap to one plan the way ops' Push to plan does, after printing the recap's
own place / notes evidence and the plan's Actuals before and after (a dry run
rolls the link back). A recap's own numbers are never touched.

    python manage.py audit_torch_plan_buckets [--since YYYY-MM-DD]
        [--attach-missing] [--fix-event-types] [--attach SPEC ...] [--apply]
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Min, Q
from django.utils import timezone

from ambassadors.models import AmbassadorEvent
from events.demo_cancel import request_display_code
from events.field_marketing import _recap_logged, is_torch_tenant
from events.models import Event, FieldMarketingEvent
from events.plan_event_types import event_type_fits_plan, event_type_for_plan
from events.plan_actuals import (
    RECAP_RELATED,
    Total,
    counts_toward_results,
    plan_actuals,
    recap_day,
)
from events.plan_results import (
    EXECUTION_LABELS,
    MARKET_STATES,
    SUGGEST_WINDOW_DAYS,
    TACTICS_FOR_EXECUTION,
    PlanResultsError,
    _ba_name,
    _day_gap,
    _recap_market,
    auto_attach_on_approval,
    build_plan_results,
    push_recap,
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
_ATTACH_RE = re.compile(r"^(custom|legacy):(\d+)=([^:]+)(?::([\w+]+))?$")
_EVIDENCE_FIELD_RE = re.compile(
    r"venue|location|event|address|store|where|note|feedback|summary|highlight|describe|comment",
    re.I,
)


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
        parser.add_argument(
            "--fix-event-types",
            action="store_true",
            help="Set plan events with no recap to the plan's program.",
        )
        parser.add_argument(
            "--attach",
            action="append",
            default=[],
            help="kind:recap_id=PLAN (REQ code or plan uuid)[:metric+metric]; repeatable.",
        )
        parser.add_argument("--apply", action="store_true", help="Write (default dry-run).")

    def handle(self, *args, **opts):
        tenants = [t for t in Tenant.objects.all().order_by("id") if is_torch_tenant(t)]
        if not tenants:
            raise CommandError("No Torch tenant found.")
        self.apply = bool(opts["apply"])
        self.attach = bool(opts["attach_missing"])
        self.fix_types = bool(opts["fix_event_types"])
        self.attach_specs = [
            piece.strip()
            for raw in opts["attach"] or []
            for piece in re.split(r"[;\s]+", raw or "")
            if piece.strip()
        ]
        writes = self.apply and (self.attach or self.fix_types or self.attach_specs)
        mode = "APPLY (writing)" if writes else "DRY-RUN (no writes)"
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
        self._attach_explicit(tenant, plans)
        self._plan_events(plans)
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

    def _find_plan(self, plans, ref: str):
        ref = ref.strip()
        for plan in plans.values():
            if plan.deleted_at is not None:
                continue
            if str(plan.uuid) == ref:
                return plan
            if plan.request_id and request_display_code(plan.request_id).lower() == ref.lower():
                return plan
        return None

    def _evidence(self, recap, kind: str) -> list[str]:
        event = recap.event
        lines = [
            f"recap name={recap.name!r} ba={_ba_name(recap)!r} day={recap_day(recap)}",
            f"event={event.id} name={event.name!r} address={event.address!r}",
        ]
        if kind == pm.KIND_CUSTOM:
            for cfv in recap.custom_field_value.select_related("custom_field").order_by(
                "custom_field__order", "custom_field_id"
            ):
                name = getattr(cfv.custom_field, "name", "") or ""
                value = (cfv.value or "").strip()
                if value and _EVIDENCE_FIELD_RE.search(name) and not value.isdigit():
                    lines.append(f"{name}: {value[:300]!r}")
        return lines

    def _actual_lines(self, plan) -> list[str]:
        payload = build_plan_results(plan, is_admin=True)
        lines = [f"counted recaps={payload['counted']} in review={payload['pending']}"]
        for row in payload["rows"]:
            actual = row.text if row.actual is None else f"{row.actual:g}"
            planned = "—" if row.planned is None else f"{row.planned:g}"
            parts = f" ({', '.join(f'{n} × {q}' for n, q in row.parts)})" if row.parts else ""
            lines.append(f"{row.label}: planned {planned} · actual {actual or '—'}{parts}")
        logged = _recap_logged(plan, plan_actuals([plan.id]).get(plan.id, {}))
        for field in ("logged_full_cans", "logged_emails", "logged_cases"):
            manual = getattr(plan, field)
            if manual is not None:
                lines.append(f"scorecard {field}={manual} (manual log — wins over recaps)")
            elif field in logged:
                lines.append(f"scorecard {field}={logged[field]} (from recaps)")
        return lines

    def _attach_explicit(self, tenant, plans) -> None:
        if not self.attach_specs:
            return
        w = self.stdout.write
        w("== Attach recaps to plans ==")
        for spec in self.attach_specs:
            m = _ATTACH_RE.match(spec)
            if not m:
                w(f"  SKIP {spec!r}: expected kind:recap_id=PLAN[:metric+metric]")
                continue
            kind, recap_id, plan_ref, keys_raw = m.group(1), int(m.group(2)), m.group(3), m.group(4)
            plan = self._find_plan(plans, plan_ref)
            if plan is None:
                w(f"  SKIP {spec}: no live plan {plan_ref!r}")
                continue
            model = CustomRecap if kind == pm.KIND_CUSTOM else Recap
            recap = model.objects.select_related(*RECAP_RELATED).filter(id=recap_id).first()
            if recap is None or recap.event.tenant_id != tenant.id:
                w(f"  SKIP {spec}: recap not found for this brand")
                continue
            execution = torch_recap_execution_type(recap)
            why_not = None
            if not _should_count(recap):
                why_not = "not approved, archived or 3rd party"
            elif plan.activity not in TACTICS_FOR_EXECUTION.get(execution, frozenset()):
                why_not = f"recap type {EXECUTION_LABELS[execution]} ≠ plan tactic {plan.activity}"
            available = pm.recap_metrics(recap, kind)
            have = {m.key for m in available}
            asked = [k for k in keys_raw.split("+") if k] if keys_raw else []
            keys = [k for k in asked if k in have] if asked else pm.default_keys(recap, available)
            w(f"  {kind} recap {recap_id} → {plan.name!r} ({plan.starts_on}, {plan.market}, {plan.activity})")
            w(f"    plan address={plan.address!r}")
            for line in self._evidence(recap, kind):
                w(f"    evidence · {line}")
            w(f"    recap values: {_fmt_metrics(available)}")
            w(f"    metrics to count: {', '.join(keys) or 'none'}")
            if set(asked) - have:
                w(f"    not on this recap (skipped): {', '.join(sorted(set(asked) - have))}")
            if why_not:
                w(f"    LEAVE: {why_not}")
                continue
            w("    plan Actuals BEFORE:")
            for line in self._actual_lines(plan):
                w(f"      {line}")
            try:
                with transaction.atomic():
                    push_recap(
                        kind=kind,
                        recap_id=recap_id,
                        plan_id=str(plan.uuid),
                        metric_keys=keys,
                        actor=None,
                        confirm_move=False,
                    )
                    w("    plan Actuals AFTER:")
                    for line in self._actual_lines(plan):
                        w(f"      {line}")
                    if not self.apply:
                        transaction.set_rollback(True)
            except PlanResultsError as exc:
                w(f"    LEAVE: {exc}")
                continue
            w("    ATTACHED" if self.apply else "    WOULD ATTACH (dry run, rolled back)")

    def _plan_events(self, plans) -> None:
        w = self.stdout.write
        w("== Plan events (program BAs get on the booked event) ==")
        live = [p for p in plans.values() if p.request_id and p.deleted_at is None]
        if not live:
            w("  none")
            return
        for plan in sorted(live, key=lambda p: (p.starts_on, p.id)):
            target = event_type_for_plan(plan)
            events = list(
                Event.objects.filter(request_id=plan.request_id)
                .select_related("event_type")
                .order_by("id")
            )
            if not events:
                w(f"  {plan.starts_on} {plan.name!r} [{plan.activity}]: no event yet")
                continue
            for event in events:
                filed = (
                    CustomRecap.objects.filter(event=event).count()
                    + Recap.objects.filter(event=event).count()
                )
                bas = AmbassadorEvent.objects.filter(event=event).count()
                fits = event_type_fits_plan(event.event_type, plan)
                program = getattr(event.event_type, "name", None) or "none"
                line = (
                    f"  {plan.starts_on} {plan.name!r} [{plan.activity}] event={event.id} "
                    f"program={program!r} bas_assigned={bas} recaps={filed}"
                )
                if fits:
                    w(line)
                    continue
                if target is None:
                    w(f"{line} WRONG_PROGRAM (no matching program for this tactic)")
                elif filed:
                    w(f"{line} WRONG_PROGRAM — LEAVE (recaps already filed)")
                elif self.fix_types and self.apply:
                    Event.objects.filter(pk=event.pk).update(event_type=target)
                    w(f"{line} WRONG_PROGRAM — FIXED → {target.name!r}")
                else:
                    w(f"{line} WRONG_PROGRAM — WOULD SET {target.name!r}")

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
