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
    "m.cristancho@liquiddeath.com": "Manuela",
    "ross@liquiddeath.com": "Ross",
    "pat@liquiddeath.com": "Pat",
    "t.reed@liquiddeath.com": "Timothy",
}
# Fixed display order for the RMM table (matches the sheet); Unassigned last.
RMM_ORDER = ["Lauren", "Kristyn", "Manuela", "Ross", "Pat", "Timothy"]
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
        return (self.willing / self.consumers * 100) if self.consumers else 0.0


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


def _pct(n: int, total: int) -> str:
    return f"{(n / total * 100):.1f}%" if total else "0.0%"


def build_summary_grid(summary: LdSummary) -> list[list]:
    """2-D values block for the rebuilt Summary tab, branded Liquid Death:
    KPI cards, then Performance by RMM / State / Month / Brand Ambassador."""
    total = summary.total_demos
    rows: list[list] = []
    rows.append(["LIQUID DEATH · RETAIL SAMPLING SUMMARY"])
    rows.append(["Auto-updated daily from Spark — Retail Samplings"])
    rows.append([])

    # KPI cards
    rows.append(
        [
            "DEMOS DONE",
            "CONSUMERS SAMPLED",
            "SINGLE CANS SOLD",
            "MULTIPACKS SOLD",
            "CONVERSION %",
        ]
    )
    rows.append(
        [
            total,
            summary.consumers,
            summary.cans,
            summary.packs,
            f"{summary.conversion_pct:.2f}%",
        ]
    )
    rows.append([])

    # Performance by RMM (Kyle's explicit ask)
    rows.append(["PERFORMANCE BY RMM"])
    rows.append(
        [
            "RMM",
            "Mapped States",
            "Retail Samplings Done",
            "% of Total",
            "Consumers Sampled",
            "Single Cans",
            "MultiPacks",
            "Conversion %",
        ]
    )
    rmm_names = [n for n in RMM_ORDER if n in summary.by_rmm]
    if summary.by_rmm.get(UNASSIGNED):
        rmm_names.append(UNASSIGNED)
    for name in rmm_names:
        b = summary.by_rmm[name]
        conv = f"{(b.willing / b.consumers * 100):.2f}%" if b.consumers else "0.00%"
        rows.append(
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
    rows.append([])

    # Performance by State
    rows.append(["PERFORMANCE BY STATE"])
    rows.append(["State", "Demos Done", "% of Total"])
    for code, demos in sorted(
        summary.by_state.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        rows.append([code, demos, _pct(demos, total)])
    rows.append([])

    # Performance by Month
    rows.append(["PERFORMANCE BY MONTH"])
    rows.append(["Month", "Demos", "Consumers Sampled", "Single Cans", "MultiPacks"])
    for month in sorted(summary.by_month.keys()):
        b = summary.by_month[month]
        rows.append([month, b.demos, b.consumers, b.cans, b.packs])
    rows.append([])

    # Performance by Brand Ambassador
    rows.append(["PERFORMANCE BY BRAND AMBASSADOR"])
    rows.append(["Brand Ambassador", "Demos Done"])
    for ba, demos in sorted(summary.by_ba.items(), key=lambda kv: (-kv[1], kv[0])):
        rows.append([ba, demos])

    return rows


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
    summary = compute_ld_summary(tenant)
    grid = build_summary_grid(summary)
    stats = {
        "demos": summary.total_demos,
        "consumers": summary.consumers,
        "cans": summary.cans,
        "packs": summary.packs,
        "conversion_pct": round(summary.conversion_pct, 2),
        "rmms": {n: summary.by_rmm[n].demos for n in summary.by_rmm},
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "rows": len(grid), **stats}

    url = (
        sheet_url
        or getattr(tenant, "recap_export_sheet_url", None)
        or getattr(tenant, "linked_sheet_url", None)
    )
    if not url:
        return {"ok": False, "error": "no-sheet-url", "tenant": getattr(tenant, "slug", None)}
    sheet_id = extract_sheet_id(url)
    if not sheet_id:
        return {"ok": False, "error": "bad-sheet-url", "url": url}
    svc = _service()
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

        # Clear the whole tab then write the rebuilt block. Only this tab.
        svc.spreadsheets().values().clear(
            spreadsheetId=sheet_id, range=f"'{actual}'!A:ZZ"
        ).execute()
        width = max((len(r) for r in grid), default=1)
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{actual}'!A1:{_col_letter(width)}{len(grid)}",
            valueInputOption="USER_ENTERED",
            body={"values": grid},
        ).execute()
        return {"ok": True, "tab": actual, "rows": len(grid), "sheet_id": sheet_id, **stats}
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
