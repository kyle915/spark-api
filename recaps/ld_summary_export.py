"""Rebuild Liquid Death's "Summary" tab daily from Spark recap data.

LD's sheet ("[External] Liquid Death (Retail) Market Schedules") has a Summary
dashboard whose KPIs (Consumers Sampled / Single Cans / MultiPacks / Conversion
%) and breakdowns (by state, month, BA, and RMM) should reflect Spark. This
module recomputes those aggregates in Python from the tenant's CustomRecaps and
writes the Summary tab as values (USER_ENTERED), refreshed daily — no fragile
in-sheet formulas, and the math always matches the in-app dashboard because it
reuses the SAME matchers (recaps.report_service._accumulate_custom).

RMM activity: each recap is attributed to an RMM by its US state, reusing
events.routing.LIQUID_DEATH_TERRITORY (the same state→RMM map the public-form
router uses, which matches the sheet's existing RMM rows exactly).

Only the Summary tab (found by name) is cleared + rewritten — every other tab
(Master Tracker, market schedules, inventory, backup) is left untouched. The
runtime service account spark-api-new-sa@spark-479222.iam.gserviceaccount.com
must have Editor access on the sheet.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from googleapiclient.errors import HttpError

from events.routing import LIQUID_DEATH_TERRITORY
from recaps.models import CustomRecap
from recaps.pdf import _event_date, _event_state
from recaps.recap_sheet_export import SERVICE_ACCOUNT_EMAIL, _tab_titles
from recaps.report_service import (
    CampaignReportKpis,
    _accumulate_custom,
    _ba_display_name,
)
from utils.sheets_mirror import _col_letter, _service, extract_sheet_id

logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_TAB = "Spark Summary"

# RMM email (from the territory map) → the first name shown in the sheet.
RMM_EMAIL_TO_NAME = {
    "l.giaccio@liquiddeath.com": "Lauren",
    "k.williams@liquiddeath.com": "Kristyn",
    # Poli shares Kristyn's territory (per Kyle). Mapped so demos assigned to
    # poli@ surface under her own row instead of Unassigned; her state-based
    # demos still fall to Kristyn (first-wins by RMM_ORDER), so no double-count.
    "poli@liquiddeath.com": "Poli",
    "m.cristancho@liquiddeath.com": "Manuela",
    "ross@liquiddeath.com": "Ross",
    "pat@liquiddeath.com": "Pat",
    "t.reed@liquiddeath.com": "Timothy",
}
# Fixed display order for the RMM table (matches the sheet); Unassigned last.
RMM_ORDER = ["Lauren", "Kristyn", "Poli", "Manuela", "Ross", "Pat", "Timothy"]
UNASSIGNED = "Unassigned"


def _build_state_to_rmm() -> dict[str, str]:
    """Invert LIQUID_DEATH_TERRITORY → {STATE: rmm_name}, first-wins so each
    demo is counted under exactly one RMM (so % of total sums to 100). DE is
    listed under both Manuela and Pat in the sheet; first-wins by RMM_ORDER
    assigns it to Manuela for counting (it has 0 demos today either way)."""
    state_to_rmm: dict[str, str] = {}
    # Iterate in RMM_ORDER so ties resolve deterministically.
    name_to_email = {v: k for k, v in RMM_EMAIL_TO_NAME.items()}
    for name in RMM_ORDER:
        email = name_to_email.get(name)
        for code in LIQUID_DEATH_TERRITORY.get(email, []):
            state_to_rmm.setdefault(code.upper(), name)
    return state_to_rmm


STATE_TO_RMM = _build_state_to_rmm()
# Display string of each RMM's mapped states (full territory, so DE shows under
# both, matching the sheet). Counting still uses STATE_TO_RMM (once).
RMM_MAPPED_STATES = {
    name: ", ".join(LIQUID_DEATH_TERRITORY.get(email, []))
    for email, name in RMM_EMAIL_TO_NAME.items()
}
# Poli isn't in the territory map (no routing change); display her territory as
# Kristyn's for the "Mapped States" column.
RMM_MAPPED_STATES["Poli"] = RMM_MAPPED_STATES.get("Kristyn", "")


def _event_rmm_name(event) -> str | None:
    """The RMM display name for an event via its assigned RMM user's email —
    the in-app dashboard's by-RMM basis. None if unassigned / not an LD RMM."""
    user = getattr(event, "rmm_asigned", None)
    email = (getattr(user, "email", "") or "").strip().lower()
    return RMM_EMAIL_TO_NAME.get(email)


@dataclass
class _Bucket:
    demos: int = 0
    consumers: int = 0
    cans: int = 0
    packs: int = 0
    willing: int = 0
    states: set = field(default_factory=set)


@dataclass
class LdSummary:
    total_demos: int = 0
    consumers: int = 0
    cans: int = 0
    packs: int = 0
    willing: int = 0
    by_rmm: dict = field(default_factory=lambda: defaultdict(_Bucket))
    by_state: dict = field(default_factory=lambda: defaultdict(int))
    by_month: dict = field(default_factory=lambda: defaultdict(_Bucket))
    by_ba: dict = field(default_factory=lambda: defaultdict(int))

    @property
    def conversion_pct(self) -> float:
        # Units sold (single cans + multipacks) per consumer sampled — matches
        # the LD sheet's "Conversion %" KPI, which sits alongside the cans +
        # packs columns. (Not willing/sampled, which can exceed 100%.)
        units = self.cans + self.packs
        return (units / self.consumers * 100) if self.consumers else 0.0


def _recap_state_code(recap) -> str | None:
    st = _event_state(recap)
    code = getattr(st, "code", None)
    if code:
        code = str(code).strip().upper()
        if len(code) >= 2:
            return code[:2]
    # Fall back to parsing the event name/address text.
    try:
        from events.routing import extract_state_code

        event = getattr(recap, "event", None)
        for src in (getattr(event, "name", None), getattr(event, "address", None)):
            if src:
                c = extract_state_code(str(src))
                if c:
                    return c
    except Exception:
        pass
    return None


def _recap_ba(recap) -> str:
    amb = getattr(recap, "ambassador", None)
    if amb is not None:
        name = _ba_display_name(amb)
        if name:
            return name
    return (getattr(recap, "external_ba_name", None) or "").strip() or "Unknown"


def _recap_month(recap) -> str | None:
    d = _event_date(recap)
    if not d:
        return None
    try:
        return d.strftime("%Y-%m")
    except Exception:
        return None


def compute_ld_summary(tenant) -> LdSummary:
    """Aggregate the tenant's CustomRecaps into the Summary model. Reuses
    report_service._accumulate_custom per recap so the KPI math matches the
    in-app dashboard and the campaign report."""
    summary = LdSummary()
    recaps = (
        CustomRecap.objects.filter(tenant=tenant)
        .select_related("event", "ambassador", "ambassador__user", "state")
        .prefetch_related(
            "custom_field_value__custom_field", "custom_recap_product_sample"
        )
    )
    for recap in recaps:
        k = CampaignReportKpis()
        _accumulate_custom(recap, k)
        consumers, cans, packs, willing = (
            k.consumers_reached,
            k.cans_sold,
            k.packs_sold,
            k.willing_to_purchase,
        )

        summary.total_demos += 1
        summary.consumers += consumers
        summary.cans += cans
        summary.packs += packs
        summary.willing += willing

        state = _recap_state_code(recap)
        rmm = STATE_TO_RMM.get(state, UNASSIGNED) if state else UNASSIGNED
        rb = summary.by_rmm[rmm]
        rb.demos += 1
        rb.consumers += consumers
        rb.cans += cans
        rb.packs += packs
        rb.willing += willing
        if state:
            rb.states.add(state)
            summary.by_state[state] += 1

        month = _recap_month(recap)
        if month:
            mb = summary.by_month[month]
            mb.demos += 1
            mb.consumers += consumers
            mb.cans += cans
            mb.packs += packs

        summary.by_ba[_recap_ba(recap)] += 1

    return summary


# ── OnBrand "RECAPS" tab — the sheet's comprehensive demo dataset spanning
#    2025–2026 (1.7k+ rows). Column indices confirmed via describe_sheet_tabs
#    --peek-tab RECAPS. We read it for the all-years headline + by-year split
#    (Spark only has 2026, so 2025 must come from here).
ONBRAND_RECAPS_TAB = "RECAPS"
_RC_DATE = 2
_RC_CONSUMERS = 10
_RC_WILLING = 13
_RC_CANS = 15
_RC_PACKS = 16


def _num(cell) -> int:
    """Parse a recap numeric cell → int; blanks / '-' / 'N/A' / junk → 0."""
    if cell is None:
        return 0
    s = str(cell).strip().replace(",", "")
    if not s or s.lower() in ("-", "—", "n/a", "na", "none"):
        return 0
    m = re.search(r"-?\d+", s)
    return int(m.group(0)) if m else 0


def _year_of(cell) -> str | None:
    m = re.search(r"(20\d\d)", str(cell or ""))
    return m.group(1) if m else None


def read_recaps_tab_by_year(svc, sheet_id: str, tab: str = ONBRAND_RECAPS_TAB) -> dict:
    """Aggregate the OnBrand RECAPS tab into per-year buckets (demos +
    consumers/cans/packs/willing). Returns {year: _Bucket}; empty dict if the
    tab is missing/unreadable (the summary then falls back to Spark-only)."""
    by_year: dict[str, _Bucket] = defaultdict(_Bucket)
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"'{tab}'!A2:Q100000")
            .execute()
        )
    except HttpError as e:
        logger.warning("ld_summary_export: RECAPS tab read failed: %s", e)
        return {}
    for row in resp.get("values") or []:
        yr = _year_of(row[_RC_DATE] if len(row) > _RC_DATE else "")
        if not yr:
            continue
        b = by_year[yr]
        b.demos += 1
        b.consumers += _num(row[_RC_CONSUMERS]) if len(row) > _RC_CONSUMERS else 0
        b.willing += _num(row[_RC_WILLING]) if len(row) > _RC_WILLING else 0
        b.cans += _num(row[_RC_CANS]) if len(row) > _RC_CANS else 0
        b.packs += _num(row[_RC_PACKS]) if len(row) > _RC_PACKS else 0
    return dict(by_year)


def _conv(b: _Bucket) -> str:
    return f"{((b.cans + b.packs) / b.consumers * 100):.1f}%" if b.consumers else "0.0%"


# ── Match the in-app Event Dashboard ────────────────────────────────────────
# The in-app dashboard (tenants/dashboard/queries.py) sums LEGACY
# ConsumerEngagements + Recap (where LD's bulk demo data lives) PLUS the
# custom-recap fold-in. compute_ld_program_kpis replicates that EXACT math so
# the sheet ties to the app (148,687 consumers / 66,401 cans / 866 events …).
# Reuses the same shared custom matchers (recaps.types) to avoid drift.
_PACK_SIZE_RE = re.compile(r"(\d+)\s*-?\s*pack", re.IGNORECASE)


def _pack_size(label: str) -> int:
    """Cans per pack from a label ("6-packs Sold" → 6); default 12 (the legacy
    Liquid Death assumption), matching tenants.dashboard.queries."""
    m = _PACK_SIZE_RE.search(label or "")
    if not m:
        return 12
    try:
        return max(1, int(m.group(1)))
    except ValueError:
        return 12


@dataclass
class ProgramKpis:
    events_run: int = 0
    consumers: int = 0
    brand_aware: int = 0
    willing: int = 0
    single_cans: int = 0
    multi_packs: int = 0
    pack_cans_equiv: int = 0
    products_sold: int = 0

    @property
    def cans_sold_total(self) -> int:
        # The app's "Cans sold" headline = single cans + pack-equivalent cans.
        return self.single_cans + self.pack_cans_equiv

    @property
    def brand_awareness_pct(self) -> float:
        return (self.brand_aware / self.consumers * 100) if self.consumers else 0.0

    @property
    def purchase_intent_pct(self) -> float:
        return (self.willing / self.consumers * 100) if self.consumers else 0.0


def compute_ld_program_kpis(tenant, year: int | None = None) -> ProgramKpis:
    """Replica of the in-app Event Dashboard KPI aggregation for `tenant`,
    optionally windowed to a calendar year. Legacy ConsumerEngagements + Recap
    sums + the custom-recap fold-in (same matchers as the dashboard)."""
    from datetime import date

    from django.db.models import Q, Sum

    from events.models import Event
    from recaps.models import ConsumerEngagements, CustomFieldValue, Recap
    from recaps.types import _consumers_sampled_from_fields, _sold_units_from_fields

    k = ProgramKpis()
    base = Event.objects.exclude(request__deleted_at__isnull=False).filter(tenant=tenant)
    if year:
        s, e = date(year, 1, 1), date(year, 12, 31)
        base = base.filter(
            Q(date__date__gte=s, date__date__lte=e)
            | Q(start_time__date__gte=s, start_time__date__lte=e)
            | Q(request__date__date__gte=s, request__date__date__lte=e)
        )
    events_with_recaps = base.filter(recaps__isnull=False).distinct()

    k.events_run = (
        base.filter(Q(recaps__isnull=False) | Q(custom_recap__isnull=False)).distinct().count()
    )
    ce = ConsumerEngagements.objects.filter(recap__event__in=events_with_recaps).aggregate(
        c=Sum("total_consumer", default=0),
        b=Sum("brand_aware_consumers", default=0),
        w=Sum("willing_to_purchase_consumers", default=0),
    )
    sales = Recap.objects.filter(event__in=events_with_recaps).aggregate(
        cans=Sum("total_cans_sold", default=0),
        packs=Sum("total_packs_sold", default=0),
        products=Sum("products_sold", default=0),
    )

    cust = {"consumers": 0, "brand": 0, "willing": 0, "cans": 0, "packs": 0, "pack_cans": 0, "products": 0}
    try:
        rows = CustomFieldValue.objects.filter(custom_recap__event__in=base).values_list(
            "custom_recap_id", "custom_field__name", "value"
        )
        by_recap: dict = {}
        for rid, name, value in rows:
            by_recap.setdefault(rid, []).append((name, value))
            low = (name or "").lower()
            digits = re.sub(r"[^\d-]", "", str(value or ""))
            if not digits or digits == "-":
                continue
            try:
                num = int(digits)
            except ValueError:
                continue
            if "knew about" in low:
                cust["brand"] += num
            elif "willing to purchase" in low and "not" not in low:
                cust["willing"] += num
            elif "single can" in low:
                cust["cans"] += num
            elif "pack" in low:
                cust["packs"] += num
                cust["pack_cans"] += num * _pack_size(low)
        for pairs in by_recap.values():
            sold = _sold_units_from_fields(pairs)
            if sold is not None:
                cust["products"] += sold
            cs = _consumers_sampled_from_fields(pairs)
            if cs is not None:
                cust["consumers"] += cs
    except Exception:  # pragma: no cover - defensive (matches dashboard's add-only fold-in)
        logger.warning("compute_ld_program_kpis: custom fold-in failed", exc_info=True)

    k.consumers = (ce["c"] or 0) + cust["consumers"]
    k.brand_aware = (ce["b"] or 0) + cust["brand"]
    k.willing = (ce["w"] or 0) + cust["willing"]
    k.single_cans = (sales["cans"] or 0) + cust["cans"]
    k.multi_packs = (sales["packs"] or 0) + cust["packs"]
    k.pack_cans_equiv = (sales["packs"] or 0) * 12 + cust["pack_cans"]
    k.products_sold = (sales["products"] or 0) + cust["products"]
    return k


def compute_ld_program_years(tenant, years: list[int]) -> dict:
    """{year: ProgramKpis} for each requested calendar year."""
    return {str(y): compute_ld_program_kpis(tenant, year=y) for y in years}


def compute_ld_program_breakdowns(tenant) -> dict:
    """Full-dataset by-RMM / by-State / by-BA breakdowns (legacy Recap +
    ConsumerEngagements + the custom-template recaps), so these tables tie to
    the in-app dashboard headline instead of the 47-recap slice. RMM is
    attributed by the recap's state via routing.LIQUID_DEATH_TERRITORY (the
    sheet's market→RMM model); state falls back recap.state → event.state."""
    from collections import defaultdict

    from django.db.models import Count, Sum

    from events.models import Event
    from recaps.models import ConsumerEngagements, Recap
    from recaps.recap_sheet_export import _ba_name

    ewr = (
        Event.objects.exclude(request__deleted_at__isnull=False)
        .filter(tenant=tenant, recaps__isnull=False)
        .distinct()
    )

    def _bucket():
        return {"events": 0, "consumers": 0, "cans": 0, "packs": 0}

    by_rmm: dict = defaultdict(_bucket)
    by_state: dict = defaultdict(_bucket)
    by_ba: dict = defaultdict(_bucket)

    # Per-recap consumer totals (legacy), summed from ConsumerEngagements.
    ce_map: dict = {}
    for row in (
        ConsumerEngagements.objects.filter(recap__event__in=ewr)
        .values("recap_id")
        .annotate(c=Sum("total_consumer", default=0))
    ):
        ce_map[row["recap_id"]] = row["c"] or 0

    for r in Recap.objects.filter(event__in=ewr).select_related(
        "state", "event", "event__state", "event__rmm_asigned",
        "ambassador", "ambassador__user",
    ):
        code = getattr(getattr(r, "state", None), "code", None) or getattr(
            getattr(getattr(r, "event", None), "state", None), "code", None
        )
        code = (str(code).strip().upper()[:2] or None) if code else None
        # RMM: the assigned RMM user (the in-app dashboard's basis) first —
        # legacy Recap.state is mostly null — then state→territory, else
        # Unassigned. `code` still feeds the by-state table.
        rmm = _event_rmm_name(getattr(r, "event", None)) or (
            STATE_TO_RMM.get(code, UNASSIGNED) if code else UNASSIGNED
        )
        cons = ce_map.get(r.id, 0)
        cans = r.total_cans_sold or 0
        packs = r.total_packs_sold or 0

        b = by_rmm[rmm]
        b["events"] += 1
        b["consumers"] += cons
        b["cans"] += cans
        b["packs"] += packs
        if code:
            s = by_state[code]
            s["events"] += 1
            s["consumers"] += cons
        ba = _ba_name(r) or "Unknown"
        ba_b = by_ba[ba]
        ba_b["events"] += 1
        ba_b["consumers"] += cons

    # Fold the custom-template recaps (the 47) into the same buckets.
    cs = compute_ld_summary(tenant)
    for name, cb in cs.by_rmm.items():
        b = by_rmm[name]
        b["events"] += cb.demos
        b["consumers"] += cb.consumers
        b["cans"] += cb.cans
        b["packs"] += cb.packs
    for code, n in cs.by_state.items():
        by_state[code]["events"] += n
    for ba, n in cs.by_ba.items():
        by_ba[ba]["events"] += n

    return {"by_rmm": dict(by_rmm), "by_state": dict(by_state), "by_ba": dict(by_ba)}


def _pct(n: int, total: int) -> str:
    return f"{(n / total * 100):.1f}%" if total else "0.0%"


# Restrained LD palette (black/white, no rainbow — per Kyle's design pref).
_BLACK = {"red": 0.05, "green": 0.05, "blue": 0.05}
_DARK = {"red": 0.16, "green": 0.16, "blue": 0.16}
_LIGHT = {"red": 0.91, "green": 0.91, "blue": 0.91}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_GRAYTEXT = {"red": 0.5, "green": 0.5, "blue": 0.5}
SUMMARY_WIDTH = 8  # widest section (the RMM table)


def build_summary_grid(
    summary: LdSummary,
    program_all: "ProgramKpis | None" = None,
    program_years: dict | None = None,
    breakdowns: dict | None = None,
) -> tuple[list[list], dict]:
    """Return (rows, layout). `layout` records which rows are the title /
    subtitle / KPI strip / section headers / table headers, so the caller can
    apply branded cell formatting.

    When `program_all` (a ProgramKpis from compute_ld_program_kpis) is given,
    the headline + PERFORMANCE BY YEAR mirror the IN-APP Event Dashboard
    (legacy ConsumerEngagements + Recap + custom) so the sheet ties to the app
    exactly; the custom-template recap breakdowns sit below, clearly labeled.
    Without it the headline is the Spark custom-recap totals (back-compat)."""
    rows: list[list] = []
    layout = {
        "title": 0,
        "subtitle": 1,
        "kpi_header": None,
        "kpi_value": None,
        "kpi_cols": 5,
        "sections": [],
        "table_headers": [],
        "ncols": SUMMARY_WIDTH,
    }

    def add(row=None) -> int:
        rows.append(list(row) if row else [])
        return len(rows) - 1

    add(["LIQUID DEATH · RETAIL SAMPLING SUMMARY"])

    if program_all is not None:
        # ── Match the in-app Spark dashboard ──
        add(["Auto-updated daily from Spark — matches the in-app dashboard"])
        add()
        layout["kpi_cols"] = 6
        layout["kpi_header"] = add(
            ["EVENTS RUN", "CONSUMERS SAMPLED", "CANS SOLD", "MULTI-PACKS SOLD",
             "BRAND AWARENESS", "PURCHASE INTENT"]
        )
        layout["kpi_value"] = add(
            [
                program_all.events_run,
                program_all.consumers,
                program_all.cans_sold_total,
                program_all.multi_packs,
                f"{program_all.brand_awareness_pct:.1f}%",
                f"{program_all.purchase_intent_pct:.1f}%",
            ]
        )
        add()

        layout["sections"].append(add(["PERFORMANCE BY YEAR"]))
        layout["table_headers"].append(
            add(["Year", "Events Run", "Consumers", "Cans Sold", "Multi-Packs",
                 "Brand Aware %", "Purchase Intent %"])
        )
        for yr in sorted((program_years or {}).keys(), reverse=True):
            p = program_years[yr]
            add([
                yr, p.events_run, p.consumers, p.cans_sold_total, p.multi_packs,
                f"{p.brand_awareness_pct:.1f}%", f"{p.purchase_intent_pct:.1f}%",
            ])
        add(["Note: Spark dates most imported history to 2026; the deeper 2025 demo "
             "dates live in the OnBrand RECAPS tab."])
        add()

        if breakdowns is None:
            # No full breakdowns supplied → the sections below are the
            # custom-template slice only; label them so they're not misread.
            layout["sections"].append(
                add([f"SPARK APP-RECAP DETAIL · {summary.total_demos} custom-template recaps"])
            )
            add()
    else:
        add(["Auto-updated daily from Spark — Retail Samplings"])
        add()
        layout["kpi_header"] = add(
            ["DEMOS DONE", "CONSUMERS SAMPLED", "SINGLE CANS SOLD", "MULTIPACKS SOLD", "CONVERSION %"]
        )
        layout["kpi_value"] = add(
            [summary.total_demos, summary.consumers, summary.cans, summary.packs,
             f"{summary.conversion_pct:.1f}%"]
        )
        add()

    if breakdowns is not None:
        # Full-dataset breakdowns (legacy + custom) — tie to the headline.
        bd_rmm = breakdowns.get("by_rmm", {})
        bd_state = breakdowns.get("by_state", {})
        bd_ba = breakdowns.get("by_ba", {})
        total_events = sum(b["events"] for b in bd_rmm.values()) or 0

        layout["sections"].append(add(["PERFORMANCE BY RMM"]))
        layout["table_headers"].append(
            add(["RMM", "Mapped States", "Demos", "% of Total", "Consumers",
                 "Single Cans", "Multi-Packs", "Conversion %"])
        )
        rmm_names = [n for n in RMM_ORDER if n in bd_rmm]
        if UNASSIGNED in bd_rmm:
            rmm_names.append(UNASSIGNED)
        for name in rmm_names:
            b = bd_rmm[name]
            conv = (
                f"{((b['cans'] + b['packs']) / b['consumers'] * 100):.1f}%"
                if b["consumers"] else "0.0%"
            )
            add([name, RMM_MAPPED_STATES.get(name, ""), b["events"],
                 _pct(b["events"], total_events), b["consumers"], b["cans"],
                 b["packs"], conv])
        add()

        layout["sections"].append(add(["PERFORMANCE BY STATE"]))
        layout["table_headers"].append(add(["State", "Demos", "% of Total", "Consumers"]))
        for code, b in sorted(bd_state.items(), key=lambda kv: (-kv[1]["events"], kv[0])):
            add([code, b["events"], _pct(b["events"], total_events), b["consumers"]])
        add()

        layout["sections"].append(add(["PERFORMANCE BY BRAND AMBASSADOR"]))
        layout["table_headers"].append(add(["Brand Ambassador", "Demos", "Consumers"]))
        for ba, b in sorted(bd_ba.items(), key=lambda kv: (-kv[1]["events"], kv[0]))[:50]:
            add([ba, b["events"], b["consumers"]])

        return rows, layout

    total = summary.total_demos

    # Performance by RMM (Kyle's explicit ask)
    layout["sections"].append(add(["PERFORMANCE BY RMM"]))
    layout["table_headers"].append(
        add(
            [
                "RMM",
                "Mapped States",
                "Retail Samplings",
                "% of Total",
                "Consumers Sampled",
                "Single Cans",
                "MultiPacks",
                "Conversion %",
            ]
        )
    )
    rmm_names = [n for n in RMM_ORDER if n in summary.by_rmm]
    if summary.by_rmm.get(UNASSIGNED):
        rmm_names.append(UNASSIGNED)
    for name in rmm_names:
        b = summary.by_rmm[name]
        conv = (
            f"{((b.cans + b.packs) / b.consumers * 100):.1f}%" if b.consumers else "0.0%"
        )
        add(
            [
                name,
                RMM_MAPPED_STATES.get(name, ""),
                b.demos,
                _pct(b.demos, total),
                b.consumers,
                b.cans,
                b.packs,
                conv,
            ]
        )
    add()

    layout["sections"].append(add(["PERFORMANCE BY STATE"]))
    layout["table_headers"].append(add(["State", "Demos Done", "% of Total"]))
    for code, demos in sorted(summary.by_state.items(), key=lambda kv: (-kv[1], kv[0])):
        add([code, demos, _pct(demos, total)])
    add()

    layout["sections"].append(add(["PERFORMANCE BY MONTH"]))
    layout["table_headers"].append(
        add(["Month", "Demos", "Consumers Sampled", "Single Cans", "MultiPacks"])
    )
    for month in sorted(summary.by_month.keys()):
        b = summary.by_month[month]
        add([month, b.demos, b.consumers, b.cans, b.packs])
    add()

    layout["sections"].append(add(["PERFORMANCE BY BRAND AMBASSADOR"]))
    layout["table_headers"].append(add(["Brand Ambassador", "Demos Done"]))
    for ba, demos in sorted(summary.by_ba.items(), key=lambda kv: (-kv[1], kv[0])):
        add([ba, demos])

    return rows, layout


def _cell(bg=None, fg=None, bold=False, italic=False, size=None, align="LEFT") -> dict:
    return {
        "userEnteredFormat": {
            "backgroundColor": bg or _WHITE,
            "horizontalAlignment": align,
            "verticalAlignment": "MIDDLE",
            "textFormat": {
                "foregroundColor": fg or _BLACK,
                "bold": bold,
                "italic": italic,
                "fontSize": size or 10,
            },
        }
    }


def _repeat(gid: int, r0: int, r1: int, c0: int, c1: int, cell: dict) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": gid,
                "startRowIndex": r0,
                "endRowIndex": r1,
                "startColumnIndex": c0,
                "endColumnIndex": c1,
            },
            "cell": cell,
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
        }
    }


def _merge(gid: int, row: int, ncols: int) -> dict:
    return {
        "mergeCells": {
            "range": {
                "sheetId": gid,
                "startRowIndex": row,
                "endRowIndex": row + 1,
                "startColumnIndex": 0,
                "endColumnIndex": ncols,
            },
            "mergeType": "MERGE_ALL",
        }
    }


def summary_format_requests(gid: int, layout: dict) -> list[dict]:
    """Sheets batchUpdate requests to brand the Spark Summary tab."""
    n = layout["ncols"]
    reqs: list[dict] = [
        # Clear any prior merges (idempotent re-runs).
        {"unmergeCells": {"range": {"sheetId": gid}}},
        # Freeze the title + subtitle.
        {
            "updateSheetProperties": {
                "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 2}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # Column widths: wide first column (names / mapped states), rest medium.
        {
            "updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 230},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": n},
                "properties": {"pixelSize": 130},
                "fields": "pixelSize",
            }
        },
        # Title bar + subtitle.
        _merge(gid, layout["title"], n),
        _merge(gid, layout["subtitle"], n),
        _repeat(gid, layout["title"], layout["title"] + 1, 0, n,
                _cell(bg=_BLACK, fg=_WHITE, bold=True, size=16, align="CENTER")),
        _repeat(gid, layout["subtitle"], layout["subtitle"] + 1, 0, n,
                _cell(bg=_WHITE, fg=_GRAYTEXT, italic=True, size=10, align="CENTER")),
        # KPI strip (kpi_cols wide — 6 for the app-matching headline, else 5).
        _repeat(gid, layout["kpi_header"], layout["kpi_header"] + 1, 0, layout.get("kpi_cols", 5),
                _cell(bg=_DARK, fg=_WHITE, bold=True, size=10, align="CENTER")),
        _repeat(gid, layout["kpi_value"], layout["kpi_value"] + 1, 0, layout.get("kpi_cols", 5),
                _cell(bg=_LIGHT, fg=_BLACK, bold=True, size=13, align="CENTER")),
    ]
    for sr in layout["sections"]:
        reqs.append(_merge(gid, sr, n))
        reqs.append(
            _repeat(gid, sr, sr + 1, 0, n, _cell(bg=_DARK, fg=_WHITE, bold=True, size=11, align="LEFT"))
        )
    for tr in layout["table_headers"]:
        reqs.append(
            _repeat(gid, tr, tr + 1, 0, n, _cell(bg=_LIGHT, fg=_BLACK, bold=True, size=10, align="LEFT"))
        )
    return reqs


def _resolve_tab(titles: list[str], wanted: str) -> str | None:
    for t in titles:
        if (t or "").strip().lower() == wanted.strip().lower():
            return t
    return None


def write_ld_summary(
    tenant,
    *,
    tab: str = DEFAULT_SUMMARY_TAB,
    target_tab: str | None = None,
    sheet_url: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Recompute + rebuild the LD Summary tab. Returns a result dict; never
    raises. `target_tab` (e.g. "Summary (staging)") writes to a scratch tab,
    creating it if missing, for eyeball before swapping to the live `tab`.
    """
    from django.utils import timezone

    summary = compute_ld_summary(tenant)  # custom-recap detail (RMM/State/Month/BA)

    # Program KPIs mirror the in-app Event Dashboard (legacy ConsumerEngagements
    # + Recap + custom) so the headline + by-year tie to the app exactly.
    program_all = compute_ld_program_kpis(tenant)
    cur = timezone.now().year
    program_years = compute_ld_program_years(tenant, [cur, cur - 1])
    breakdowns = compute_ld_program_breakdowns(tenant)

    grid, layout = build_summary_grid(
        summary, program_all=program_all, program_years=program_years, breakdowns=breakdowns
    )
    stats = {
        "events_run": program_all.events_run,
        "consumers": program_all.consumers,
        "cans_sold": program_all.cans_sold_total,
        "single_cans": program_all.single_cans,
        "multi_packs": program_all.multi_packs,
        "brand_awareness_pct": round(program_all.brand_awareness_pct, 1),
        "purchase_intent_pct": round(program_all.purchase_intent_pct, 1),
        "app_recaps": summary.total_demos,
        "by_year": {
            y: {
                "events_run": p.events_run,
                "consumers": p.consumers,
                "cans_sold": p.cans_sold_total,
                "multi_packs": p.multi_packs,
            }
            for y, p in sorted(program_years.items())
        },
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "rows": len(grid), **stats}

    url = (
        sheet_url
        or getattr(tenant, "recap_export_sheet_url", None)
        or getattr(tenant, "linked_sheet_url", None)
    )
    sheet_id = extract_sheet_id(url) if url else None
    svc = _service()
    if not url:
        return {"ok": False, "error": "no-sheet-url", "tenant": getattr(tenant, "slug", None)}
    if not sheet_id:
        return {"ok": False, "error": "bad-sheet-url", "url": url}
    if svc is None:
        return {"ok": False, "error": "no-credentials"}

    try:
        titles = _tab_titles(svc, sheet_id)
        wanted = (target_tab or tab).strip()
        actual = _resolve_tab(titles, wanted)
        if actual is None:
            # "Spark Summary" is a dedicated, Spark-owned tab — create it if it
            # doesn't exist yet. Creating a new tab never touches existing tabs.
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": wanted}}}]},
            ).execute()
            actual = wanted

        # Resolve the tab's gid up front (needed to wipe stale formatting).
        meta = (
            svc.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="sheets.properties(title,sheetId)")
            .execute()
        )
        gid = next(
            (
                s["properties"]["sheetId"]
                for s in meta.get("sheets", [])
                if s.get("properties", {}).get("title") == actual
            ),
            None,
        )

        # Clear values, then WIPE all cell formatting before writing. clear()
        # only clears values — leftover formats from a prior layout (e.g. an
        # old "% of Total" column now sitting under "Single Cans") would force
        # a wrong percent display onto the new numbers. Resetting userEntered
        # format to default first, then USER_ENTERED writes, lets Sheets
        # auto-format each value (plain numbers; "22.6%" strings → percent).
        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"'{actual}'!A:ZZ"
        ).execute()
        if gid is not None:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [
                    {"repeatCell": {"range": {"sheetId": gid}, "cell": {}, "fields": "userEnteredFormat"}}
                ]},
            ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{actual}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": grid},
        ).execute()

        # Apply branded formatting (non-fatal — values are already written).
        formatted = False
        try:
            if gid is not None:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": summary_format_requests(gid, layout)},
                ).execute()
                formatted = True
        except Exception as fe:  # pragma: no cover - formatting is best-effort
            logger.warning("ld_summary_export: formatting failed (values written): %s", fe)

        return {
            "ok": True,
            "tab": actual,
            "rows": len(grid),
            "formatted": formatted,
            "sheet_id": sheet_id,
            **stats,
        }
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        if str(status) == "403":
            detail = (
                f"Sheets API 403 — share the sheet with {SERVICE_ACCOUNT_EMAIL} (Editor)."
            )
        else:
            detail = " ".join(str(e).split())[:400]
        logger.warning("ld_summary_export: write failed (status=%s): %s", status, e)
        return {"ok": False, "error": "sheets-api", "status": status, "detail": detail}
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("ld_summary_export: unexpected failure")
        return {"ok": False, "error": "unexpected", "detail": " ".join(str(e).split())[:400]}
