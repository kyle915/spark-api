"""Run the hours check for one client + pay period, and record reviews."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date

from hours_check.facts import ShiftFacts, gather
from hours_check.rules import RULE_TITLES, SEVERITY_ORDER, Flag, check_shift
from hours_check.thresholds import thresholds


@dataclass
class CheckedFlag:
    flag: Flag
    reviewed: bool = False
    # A review exists but was for different facts (a punch changed since).
    stale_review: bool = False
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str = ""


@dataclass
class CheckedShift:
    facts: ShiftFacts
    flags: list[CheckedFlag] = field(default_factory=list)

    @property
    def needs_look(self) -> bool:
        return any(not f.reviewed for f in self.flags)


@dataclass
class CheckReport:
    start: _date
    end: _date
    total_shifts: int
    shifts: list[CheckedShift]
    thresholds: dict[str, float]

    @property
    def flagged_shifts(self) -> int:
        return len(self.shifts)

    @property
    def needs_look_shifts(self) -> int:
        return sum(1 for s in self.shifts if s.needs_look)

    def by_rule(self) -> list[tuple[str, str, int, int]]:
        """[(code, worst severity, flags, unreviewed)] in rule order."""
        counts: dict[str, list] = {}
        for s in self.shifts:
            for cf in s.flags:
                c = counts.setdefault(cf.flag.code, [cf.flag.severity, 0, 0])
                if SEVERITY_ORDER[cf.flag.severity] < SEVERITY_ORDER[c[0]]:
                    c[0] = cf.flag.severity
                c[1] += 1
                if not cf.reviewed:
                    c[2] += 1
        return [
            (code, counts[code][0], counts[code][1], counts[code][2])
            for code in RULE_TITLES
            if code in counts
        ]


def _user_label(u) -> str | None:
    if u is None:
        return None
    name = f"{u.first_name or ''} {u.last_name or ''}".strip()
    return name or u.email or None


def run_check(tenant_id: int, start: _date, end: _date) -> CheckReport:
    from hours_check.models import HoursFlagReview

    th = thresholds()
    facts = gather(tenant_id, start, end)
    reviews = {}
    if facts:
        for r in HoursFlagReview.objects.filter(
            tenant_id=tenant_id, event_id__in={f.event_id for f in facts}
        ).select_related("reviewed_by"):
            reviews[(r.event_id, r.ambassador_id, r.code)] = r

    shifts: list[CheckedShift] = []
    for sf in facts:
        flags = check_shift(sf, th)
        if not flags:
            continue
        checked = []
        for fl in flags:
            r = reviews.get((sf.event_id, sf.ambassador_id, fl.code))
            cf = CheckedFlag(flag=fl)
            if r is not None:
                if r.fingerprint == fl.fingerprint:
                    cf.reviewed = True
                    cf.reviewed_by = _user_label(r.reviewed_by)
                    cf.reviewed_at = r.reviewed_at.isoformat() if r.reviewed_at else None
                    cf.review_note = r.note or ""
                else:
                    cf.stale_review = True
            checked.append(cf)
        shifts.append(CheckedShift(facts=sf, flags=checked))

    # Shifts that still need a look first, then by worst severity, then time.
    shifts.sort(
        key=lambda s: (
            not s.needs_look,
            min(SEVERITY_ORDER[f.flag.severity] for f in s.flags),
            s.facts.local_date or start,
            s.facts.ambassador_name.lower(),
        )
    )
    return CheckReport(start, end, len(facts), shifts, th)


def current_flag(tenant_id: int, event, ambassador_id: int, code: str) -> Flag | None:
    """The flag ``code`` as it fires RIGHT NOW for one shift (server-side
    fingerprint for a review; the client's copy is never trusted)."""
    from events.venue_day import venue_local_date

    day = event.date.date() if event.date else venue_local_date(event)
    if day is None:
        return None
    th = thresholds()
    for sf in gather(tenant_id, day, day):
        if sf.event_id == event.id and sf.ambassador_id == ambassador_id:
            for fl in check_shift(sf, th):
                if fl.code == code:
                    return fl
    return None
