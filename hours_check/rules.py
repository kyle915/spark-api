"""The hours-check rules: fixed, explainable tests over one shift's facts.

Every rule reads a ``ShiftFacts`` (``hours_check.facts``) plus the
thresholds (``hours_check.thresholds``) and returns a ``Flag`` or nothing.
A flag is advisory: it carries a severity, the facts it judged, and a
suggested action. Nothing here approves, rejects or edits anything — staff
decide. Rules that cannot judge (missing booked window, no GPS fix) stay
quiet rather than guessing; the one exception is ``no_gps``, whose whole
point is the missing fix.

Severity: ``high`` (likely money at stake, look before approving),
``medium`` (worth a look), ``low`` (context; often fine).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hours_check.facts import ShiftFacts, haversine_miles

HIGH, MEDIUM, LOW = "high", "medium", "low"
SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}

# code -> human title (stable; the FE shows it as the chip label)
RULE_TITLES: dict[str, str] = {
    "over_booked": "Worked more than booked",
    "unverified_hours": "Hours not from the clock",
    "auto_closed": "Clock-out filled in automatically",
    "far_clock_in": "Clocked in away from the venue",
    "far_clock_out": "Clocked out away from the venue",
    "no_gps": "No GPS on the punches",
    "early_clock_in": "Clocked in long before the start",
    "late_clock_out": "Clocked out long after the end",
    "overlap": "Overlaps another shift",
    "impossible_travel": "Travel time looks impossible",
    "recap_before_clock_out": "Recap filed before the shift ended",
    "recap_far_away": "Recap filed away from the venue",
    "no_recap": "No recap for a paid shift",
    "edited_punch": "Punch added or edited by staff",
    "duplicate_punches": "Extra clock-ins or clock-outs",
    "round_times": "Punch times look typed in",
    "long_shift": "Very long shift",
    "mileage_mismatch": "Mileage claim above GPS miles",
}


@dataclass
class Flag:
    code: str
    severity: str
    summary: str
    action: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    # Short, stable summary of the judged facts. A "reviewed" mark holds only
    # while this matches, so a punch edit that changes the facts asks again.
    fingerprint: str = ""
    # True when the flag's facts include GPS / location detail.
    uses_location: bool = False

    @property
    def title(self) -> str:
        return RULE_TITLES.get(self.code, self.code)


def _local(dt: datetime | None, zone: str) -> str:
    if dt is None:
        return "—"
    try:
        z = ZoneInfo(zone)
    except Exception:  # noqa: BLE001
        z = ZoneInfo("America/Los_Angeles")
    d = dt.astimezone(z)
    return d.strftime("%-I:%M %p ") + (d.tzname() or "")


def _h(hours: float) -> str:
    return f"{hours:.2f} h"


def _mi(miles: float) -> str:
    return f"{miles:.1f} mi"


def _min(minutes: float) -> str:
    m = int(round(minutes))
    if m >= 90:
        return f"{m // 60} h {m % 60} min"
    return f"{m} min"


# ---------------------------------------------------------------------------
# Single-shift rules
# ---------------------------------------------------------------------------
def over_booked(sf: ShiftFacts, th) -> Flag | None:
    if sf.estimated or sf.worked_hours is None or sf.scheduled_hours is None:
        return None
    allowed = sf.scheduled_hours + sf.extension_minutes / 60.0
    over_min = (sf.worked_hours - allowed) * 60.0
    if over_min <= th["over_tolerance_minutes"]:
        return None
    sev = HIGH if over_min >= 60 else MEDIUM
    return Flag(
        "over_booked",
        sev,
        f"Paid {_h(sf.worked_hours)} on a {_h(sf.scheduled_hours)} booking"
        + (f" (+{sf.extension_minutes} min approved extension)" if sf.extension_minutes else "")
        + f", {_min(over_min)} over.",
        "Confirm the extra time with the BA or the RMM. If they left on time, "
        "edit the clock-out before approving.",
        [
            ("Worked", _h(sf.worked_hours)),
            ("Booked", _h(sf.scheduled_hours)),
            ("Approved extension", f"{sf.extension_minutes} min"),
            ("Over by", _min(over_min)),
        ],
        f"worked={sf.worked_hours:.2f};booked={sf.scheduled_hours:.2f};ext={sf.extension_minutes}",
    )


def unverified_hours(sf: ShiftFacts, th) -> Flag | None:
    if not sf.estimated or not sf.shift_over:
        return None
    if sf.on_the_clock:
        what = "Clocked in but never clocked out"
    elif not sf.punches:
        what = "No clock-in or clock-out at all"
    else:
        what = "No complete clock-in/clock-out pair"
    paid = (
        f"paid the scheduled {_h(sf.worked_hours)}"
        if sf.worked_hours is not None
        else "no hours to pay"
    )
    return Flag(
        "unverified_hours",
        HIGH if not sf.punches else MEDIUM,
        f"{what}; {paid}.",
        "Check the recap, photos or the RMM that the BA worked this shift "
        "before paying it, or add the missing punch.",
        [("Punches", str(len(sf.punches))), ("Paid hours", _h(sf.worked_hours) if sf.worked_hours is not None else "—")],
        f"punches={len(sf.punches)};open={int(sf.on_the_clock)}",
    )


def auto_closed(sf: ShiftFacts, th) -> Flag | None:
    auto = [p for p in sf.punches if p.auto_closed]
    if not auto:
        return None
    return Flag(
        "auto_closed",
        MEDIUM,
        f"The BA never clocked out; Spark closed the shift at its scheduled end "
        f"({_local(auto[-1].at, sf.time_zone)}).",
        "Ask the BA when they actually left. Edit the clock-out if it was "
        "earlier; leave it if they worked to the end.",
        [("Auto clock-out at", _local(auto[-1].at, sf.time_zone))],
        f"auto={auto[-1].at.isoformat()}",
    )


def _far(sf: ShiftFacts, th, kind: str) -> Flag | None:
    if sf.venue is None:
        return None
    punches = [p for p in sf.punches if p.kind == kind and p.coords and not p.auto_closed]
    if not punches:
        return None
    p = punches[0] if kind == "clock_in" else punches[-1]
    miles = haversine_miles(p.coords, sf.venue)
    if miles <= th["far_miles"]:
        return None
    code = "far_clock_in" if kind == "clock_in" else "far_clock_out"
    word = "in" if kind == "clock_in" else "out"
    sev = HIGH if miles >= max(10.0, th["far_miles"] * 10) else MEDIUM
    return Flag(
        code,
        sev,
        f"Clocked {word} {_mi(miles)} from the venue"
        + (" (venue position looked up from the address)" if sf.venue_source == "address lookup" else "")
        + ".",
        "Check the venue address and the BA's photos. A wrong store, a mall "
        "or a bad GPS fix explains some; clocking from home does not.",
        [
            (f"Clock-{word} distance", _mi(miles)),
            (f"Clock-{word} time", _local(p.at, sf.time_zone)),
            ("Venue position from", sf.venue_source or "—"),
        ],
        f"{code}={miles:.1f}",
        uses_location=True,
    )


def far_clock_in(sf, th):
    return _far(sf, th, "clock_in")


def far_clock_out(sf, th):
    return _far(sf, th, "clock_out")


def no_gps(sf: ShiftFacts, th) -> Flag | None:
    ba_punches = [p for p in sf.punches if not p.auto_closed and not p.created_by_id]
    if not ba_punches:
        return None
    if any(p.coords for p in ba_punches) or sf.has_pings:
        return None
    return Flag(
        "no_gps",
        LOW,
        "The BA's punches have no GPS fix and the phone sent no location pings.",
        "Usually location permission was off. Check photos or the recap for "
        "proof they were on site.",
        [("Punches without GPS", str(len(ba_punches)))],
        f"n={len(ba_punches)}",
        uses_location=True,
    )


def early_clock_in(sf: ShiftFacts, th) -> Flag | None:
    if sf.booked_start is None or sf.first_in is None:
        return None
    early = (sf.booked_start - sf.first_in).total_seconds() / 60.0
    if early <= th["early_minutes"]:
        return None
    padded = (
        sf.worked_hours is not None
        and not sf.estimated
        and sf.scheduled_hours is not None
        and sf.worked_hours > sf.scheduled_hours + sf.extension_minutes / 60.0
    )
    return Flag(
        "early_clock_in",
        MEDIUM if padded else LOW,
        f"Clocked in {_min(early)} before the booked start "
        f"({_local(sf.first_in, sf.time_zone)} vs {_local(sf.booked_start, sf.time_zone)}).",
        "Ask whether setup was agreed. If not, move the clock-in to the booked start.",
        [
            ("Clock-in", _local(sf.first_in, sf.time_zone)),
            ("Booked start", _local(sf.booked_start, sf.time_zone)),
            ("Early by", _min(early)),
        ],
        f"in={sf.first_in.isoformat()};start={sf.booked_start.isoformat()}",
    )


def late_clock_out(sf: ShiftFacts, th) -> Flag | None:
    if sf.booked_end is None or sf.last_out is None or sf.on_the_clock:
        return None
    if any(p.auto_closed and p.at == sf.last_out for p in sf.punches):
        return None
    end = sf.booked_end + timedelta(minutes=sf.extension_minutes)
    late = (sf.last_out - end).total_seconds() / 60.0
    if late <= th["late_minutes"]:
        return None
    padded = (
        sf.worked_hours is not None
        and not sf.estimated
        and sf.scheduled_hours is not None
        and sf.worked_hours > sf.scheduled_hours + sf.extension_minutes / 60.0
    )
    return Flag(
        "late_clock_out",
        MEDIUM if padded else LOW,
        f"Clocked out {_min(late)} after the booked end"
        + (" and approved extension" if sf.extension_minutes else "")
        + f" ({_local(sf.last_out, sf.time_zone)} vs {_local(end, sf.time_zone)}).",
        "Ask whether they stayed late on purpose. A forgotten clock-out reads "
        "the same; edit it to when they left.",
        [
            ("Clock-out", _local(sf.last_out, sf.time_zone)),
            ("Booked end (+ extension)", _local(end, sf.time_zone)),
            ("Late by", _min(late)),
        ],
        f"out={sf.last_out.isoformat()};end={end.isoformat()}",
    )


def recap_before_clock_out(sf: ShiftFacts, th) -> Flag | None:
    if sf.recap_submitted_at is None or sf.last_out is None or sf.on_the_clock:
        return None
    # An auto-closed clock-out still counts here: the recap time is then the
    # best clue to when the BA actually left.
    early = (sf.last_out - sf.recap_submitted_at).total_seconds() / 60.0
    if early <= th["recap_early_minutes"]:
        return None
    return Flag(
        "recap_before_clock_out",
        HIGH if early >= 120 else MEDIUM,
        f"Recap filed {_min(early)} before the clock-out "
        f"({_local(sf.recap_submitted_at, sf.time_zone)} vs {_local(sf.last_out, sf.time_zone)}).",
        "The BA may have wrapped up early. Ask them; if so, move the clock-out "
        "to about when the recap was filed.",
        [
            ("Recap filed", _local(sf.recap_submitted_at, sf.time_zone)),
            ("Clock-out", _local(sf.last_out, sf.time_zone)),
            ("Gap", _min(early)),
        ],
        f"recap={sf.recap_submitted_at.isoformat()};out={sf.last_out.isoformat()}",
    )


def recap_far_away(sf: ShiftFacts, th) -> Flag | None:
    if sf.recap_ping_miles is None or sf.recap_ping_minutes is None:
        return None
    if sf.recap_ping_minutes > th["recap_ping_window_minutes"]:
        return None
    if sf.recap_ping_miles <= th["far_miles"]:
        return None
    return Flag(
        "recap_far_away",
        MEDIUM,
        f"The phone was {_mi(sf.recap_ping_miles)} from the venue when the recap was filed.",
        "Filing from the car or home is common; worth a look only with other "
        "flags on this shift.",
        [
            ("Distance at filing", _mi(sf.recap_ping_miles)),
            ("Nearest GPS ping", f"{_min(sf.recap_ping_minutes)} from the submit"),
        ],
        f"mi={sf.recap_ping_miles:.1f}",
        uses_location=True,
    )


def no_recap(sf: ShiftFacts, th) -> Flag | None:
    if sf.has_recap or not sf.shift_over:
        return None
    return Flag(
        "no_recap",
        MEDIUM,
        "No recap was filed for this shift.",
        "Ask the BA to file the recap before paying, or confirm the shift "
        "did not need one.",
        [("Recap", "none")],
        "none",
    )


def edited_punch(sf: ShiftFacts, th) -> Flag | None:
    touched = [
        p for p in sf.punches
        if (p.created_by_id and p.created_by_id != sf.ba_user_id)
        or (p.updated_by_id and p.updated_by_id != sf.ba_user_id)
    ]
    if not touched:
        return None
    facts = []
    for p in touched:
        if p.created_by_id and p.created_by_id != sf.ba_user_id:
            who = sf.editors.get(p.created_by_id, f"user {p.created_by_id}")
            facts.append((f"{p.kind.replace('_', '-')} added by", f"{who}, {_local(p.created_at, sf.time_zone)}"))
        elif p.updated_by_id:
            who = sf.editors.get(p.updated_by_id, f"user {p.updated_by_id}")
            facts.append((f"{p.kind.replace('_', '-')} edited by", f"{who}, {_local(p.updated_at, sf.time_zone)}"))
    return Flag(
        "edited_punch",
        LOW,
        f"{len(touched)} punch{'es' if len(touched) != 1 else ''} added or changed by staff, not tapped by the BA.",
        "Usually a fix someone made on purpose. Check the note on the "
        "request's activity log if the hours look off.",
        facts,
        ";".join(f"{p.kind}:{p.at.isoformat()}" for p in touched),
    )


def duplicate_punches(sf: ShiftFacts, th) -> Flag | None:
    ins = sum(1 for p in sf.punches if p.kind == "clock_in")
    outs = sum(1 for p in sf.punches if p.kind == "clock_out")
    if ins <= 1 and outs <= 1:
        return None
    return Flag(
        "duplicate_punches",
        LOW,
        f"{ins} clock-ins and {outs} clock-outs on one shift. Paid hours run "
        "from the first clock-in to the last clock-out, so breaks in between are paid.",
        "Check whether the BA clocked out for a break or tapped twice.",
        [("Clock-ins", str(ins)), ("Clock-outs", str(outs))],
        f"in={ins};out={outs}",
    )


def round_times(sf: ShiftFacts, th) -> Flag | None:
    if sf.first_in is None or sf.last_out is None or sf.on_the_clock:
        return None
    pins = [p for p in sf.punches if p.at in (sf.first_in, sf.last_out) and not p.auto_closed]
    if len(pins) < 2:
        return None

    def _round(dt):
        return dt.second == 0 and dt.microsecond == 0 and dt.minute % 15 == 0

    if not all(_round(p.at) for p in pins):
        return None
    return Flag(
        "round_times",
        LOW,
        f"Both punches land exactly on the quarter hour "
        f"({_local(sf.first_in, sf.time_zone)} to {_local(sf.last_out, sf.time_zone)}), "
        "which a phone tap almost never does.",
        "These were probably typed in. Check who entered them and why.",
        [("Clock-in", _local(sf.first_in, sf.time_zone)), ("Clock-out", _local(sf.last_out, sf.time_zone))],
        f"in={sf.first_in.isoformat()};out={sf.last_out.isoformat()}",
    )


def long_shift(sf: ShiftFacts, th) -> Flag | None:
    if sf.worked_hours is None or sf.estimated:
        return None
    if sf.worked_hours <= th["long_shift_hours"]:
        return None
    return Flag(
        "long_shift",
        HIGH if sf.worked_hours >= 14 else MEDIUM,
        f"{_h(sf.worked_hours)} on the clock in one shift.",
        "Check for a forgotten clock-out and any overtime or break rules.",
        [("Worked", _h(sf.worked_hours)), ("Limit", _h(th["long_shift_hours"]))],
        f"worked={sf.worked_hours:.2f}",
    )


def mileage_mismatch(sf: ShiftFacts, th) -> Flag | None:
    if sf.claim_miles is None or sf.gps_miles is None or sf.claim_miles <= 0:
        return None
    over = sf.claim_miles - sf.gps_miles
    if over <= th["mileage_over_miles"] or over <= sf.gps_miles * th["mileage_over_ratio"]:
        return None
    return Flag(
        "mileage_mismatch",
        MEDIUM,
        f"Mileage claim of {_mi(sf.claim_miles)} against {_mi(sf.gps_miles)} tracked by GPS.",
        "Compare the claimed stops with the tracked drive before paying mileage.",
        [("Claimed", _mi(sf.claim_miles)), ("GPS tracked", _mi(sf.gps_miles)), ("Difference", _mi(over))],
        f"claim={sf.claim_miles:.1f};gps={sf.gps_miles:.1f}",
        uses_location=True,
    )


# ---------------------------------------------------------------------------
# Cross-shift rules (the same BA on other events, any client)
# ---------------------------------------------------------------------------
def overlap(sf: ShiftFacts, th) -> Flag | None:
    if sf.first_in is None or sf.last_out is None or sf.on_the_clock:
        return None
    hits = []
    for o in sf.others:
        lo = max(sf.first_in, o.first_in)
        hi = min(sf.last_out, o.last_out)
        mins = (hi - lo).total_seconds() / 60.0
        if mins > th["overlap_minutes"]:
            hits.append((o, mins))
    if not hits:
        return None
    o, mins = max(hits, key=lambda x: x[1])
    return Flag(
        "overlap",
        HIGH,
        f"On the clock at another shift ({o.event_name or 'another event'}) "
        f"for {_min(mins)} of this one.",
        "Only one of these can be paid for that time. Fix the punches on the "
        "shift they were not at.",
        [
            ("Other shift", o.event_name or "—"),
            ("Other shift on the clock", f"{_local(o.first_in, sf.time_zone)} to {_local(o.last_out, sf.time_zone)}"),
            ("Overlap", _min(mins)),
        ],
        f"other={o.event_id};mins={int(mins)}",
    )


def impossible_travel(sf: ShiftFacts, th) -> Flag | None:
    """Arriving here too soon after clocking out somewhere far away."""
    if sf.first_in is None or sf.venue is None:
        return None
    before = [
        o for o in sf.others
        if o.venue is not None and o.last_out <= sf.first_in
        and sf.first_in - o.last_out < timedelta(hours=12)
    ]
    if not before:
        return None
    prev = max(before, key=lambda o: o.last_out)
    miles = haversine_miles(prev.venue, sf.venue)
    if miles < th["travel_min_miles"]:
        return None
    gap_h = (sf.first_in - prev.last_out).total_seconds() / 3600.0
    mph = miles / gap_h if gap_h > 0 else float("inf")
    if mph <= th["max_travel_mph"]:
        return None
    return Flag(
        "impossible_travel",
        HIGH,
        f"Clocked in here {_min(gap_h * 60)} after clocking out of "
        f"{prev.event_name or 'another shift'}, {_mi(miles)} away "
        f"({'over ' + str(int(mph)) + ' mph' if mph != float('inf') else 'no time between'}).",
        "One of the two punches is probably wrong. Check both shifts' times "
        "and GPS before paying either.",
        [
            ("Previous shift", prev.event_name or "—"),
            ("Previous clock-out", _local(prev.last_out, sf.time_zone)),
            ("This clock-in", _local(sf.first_in, sf.time_zone)),
            ("Straight-line distance", _mi(miles)),
        ],
        f"prev={prev.event_id};mi={miles:.0f};gap={int(gap_h * 60)}",
        uses_location=True,
    )


ALL_RULES = (
    over_booked,
    unverified_hours,
    auto_closed,
    far_clock_in,
    far_clock_out,
    no_gps,
    early_clock_in,
    late_clock_out,
    overlap,
    impossible_travel,
    recap_before_clock_out,
    recap_far_away,
    no_recap,
    edited_punch,
    duplicate_punches,
    round_times,
    long_shift,
    mileage_mismatch,
)


def check_shift(sf: ShiftFacts, th) -> list[Flag]:
    flags: list[Flag] = []
    for rule in ALL_RULES:
        try:
            f = rule(sf, th)
        except Exception:  # noqa: BLE001 — one bad row never hides the rest
            f = None
        if f is not None:
            flags.append(f)
    flags.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    return flags
