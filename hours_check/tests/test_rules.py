"""Each hours-check rule against hand-built shift facts (no database)."""

from datetime import datetime, timedelta, timezone

import pytest

from hours_check import rules
from hours_check.facts import OtherShift, Punch, ShiftFacts
from hours_check.thresholds import DEFAULTS

UTC = timezone.utc
T0 = datetime(2026, 11, 10, 17, 0, 7, tzinfo=UTC)  # 9:00:07 AM Pacific
VENUE = (30.2672, -97.7431)  # Austin
NEAR = (30.2680, -97.7440)  # ~0.08 mi
FAR = (30.4000, -97.7431)  # ~9 mi north
TH = dict(DEFAULTS)


def punch(kind, at, coords=NEAR, **kw):
    return Punch(
        kind=kind, at=at, coords=coords,
        created_by_id=kw.get("cby"), updated_by_id=kw.get("uby"),
        created_at=at, updated_at=at, auto_closed=kw.get("auto", False),
    )


def shift(hours=4.0, worked=None, **kw) -> ShiftFacts:
    """A clean 4-hour shift worked exactly as booked, unless overridden."""
    worked = hours if worked is None else worked
    first_in = kw.pop("first_in", T0)
    last_out = kw.pop("last_out", first_in + timedelta(hours=worked))
    punches = kw.pop(
        "punches",
        [punch("clock_in", first_in), punch("clock_out", last_out)],
    )
    sf = ShiftFacts(
        event_id=1, event_uuid="e1", event_name="HEB #614", tenant_id=1,
        ambassador_id=1, ambassador_uuid="a1", ambassador_name="Ana", ba_user_id=50,
        time_zone="America/Chicago",
        booked_start=kw.pop("booked_start", T0),
        booked_end=kw.pop("booked_end", T0 + timedelta(hours=hours)),
        scheduled_hours=hours,
        punches=punches,
        first_in=first_in, last_out=last_out,
        worked_hours=round((last_out - first_in).total_seconds() / 3600, 2),
        estimated=False,
        venue=VENUE, venue_source="event", has_pings=True, has_recap=True,
        recap_submitted_at=last_out - timedelta(minutes=5),
    )
    for k, v in kw.items():
        setattr(sf, k, v)
    return sf


def codes(sf):
    return [f.code for f in rules.check_shift(sf, TH)]


def test_clean_shift_has_no_flags():
    assert codes(shift()) == []


# --- over_booked -------------------------------------------------------------
def test_over_booked_flags_past_tolerance():
    f = rules.over_booked(shift(worked=4.5), TH)
    assert f and f.severity == rules.MEDIUM
    assert "30 min over" in f.summary


def test_over_booked_quiet_within_tolerance():
    assert rules.over_booked(shift(worked=4.2), TH) is None  # 12 min


def test_over_booked_counts_approved_extension():
    assert rules.over_booked(shift(worked=5.0, extension_minutes=60), TH) is None
    f = rules.over_booked(shift(worked=6.0, extension_minutes=30), TH)
    assert f and f.severity == rules.HIGH  # 90 min over


def test_over_booked_fingerprint_changes_with_hours():
    a = rules.over_booked(shift(worked=5), TH).fingerprint
    b = rules.over_booked(shift(worked=6), TH).fingerprint
    assert a != b


# --- unverified / auto-closed ------------------------------------------------
def test_unverified_when_no_punches():
    sf = shift(punches=[], estimated=True)
    sf.first_in = sf.last_out = None
    f = rules.unverified_hours(sf, TH)
    assert f and f.severity == rules.HIGH


def test_unverified_when_still_on_the_clock():
    sf = shift(estimated=True, on_the_clock=True)
    f = rules.unverified_hours(sf, TH)
    assert f and f.severity == rules.MEDIUM and "never clocked out" in f.summary


def test_auto_closed():
    end = T0 + timedelta(hours=4)
    sf = shift(punches=[punch("clock_in", T0), punch("clock_out", end, coords=None, auto=True)])
    assert "auto_closed" in codes(sf)
    # The auto clock-out sits at the booked end, so it is not "late".
    assert rules.late_clock_out(sf, TH) is None


# --- distance / GPS ------------------------------------------------------------
def test_far_clock_in_and_out():
    sf = shift(punches=[punch("clock_in", T0, FAR), punch("clock_out", T0 + timedelta(hours=4), FAR)])
    got = codes(sf)
    assert "far_clock_in" in got and "far_clock_out" in got
    assert rules.far_clock_in(sf, TH).uses_location


def test_far_quiet_without_venue_position():
    sf = shift(venue=None, punches=[punch("clock_in", T0, FAR), punch("clock_out", T0 + timedelta(hours=4), FAR)])
    assert rules.far_clock_in(sf, TH) is None


def test_no_gps_only_when_no_fix_and_no_pings():
    sf = shift(
        punches=[punch("clock_in", T0, None), punch("clock_out", T0 + timedelta(hours=4), None)],
        has_pings=False,
    )
    assert rules.no_gps(sf, TH) is not None
    sf.has_pings = True
    assert rules.no_gps(sf, TH) is None


# --- window ------------------------------------------------------------------
def test_early_clock_in_low_when_hours_not_padded():
    first_in = T0 - timedelta(minutes=90)
    sf = shift(first_in=first_in, last_out=T0 + timedelta(hours=2, minutes=30))
    f = rules.early_clock_in(sf, TH)
    assert f and f.severity == rules.LOW


def test_early_clock_in_medium_when_padded():
    first_in = T0 - timedelta(minutes=90)
    sf = shift(first_in=first_in, last_out=T0 + timedelta(hours=4))
    assert rules.early_clock_in(sf, TH).severity == rules.MEDIUM


def test_late_clock_out_respects_extension():
    out = T0 + timedelta(hours=5, minutes=30)
    sf = shift(last_out=out)
    assert rules.late_clock_out(sf, TH) is not None
    sf.extension_minutes = 60
    assert rules.late_clock_out(sf, TH) is None


# --- cross-shift ------------------------------------------------------------
def test_overlap_with_other_shift():
    sf = shift()
    sf.others = [OtherShift(2, "e2", "Target #9", 2, T0 + timedelta(hours=1), T0 + timedelta(hours=6), VENUE)]
    f = rules.overlap(sf, TH)
    assert f and f.severity == rules.HIGH and "3 h 0 min" in f.summary


def test_impossible_travel():
    sf = shift()
    # Clocked out 60 mi away 20 minutes before this clock-in.
    far_venue = (31.1, -97.7431)
    sf.others = [OtherShift(2, "e2", "Walmart Temple", 2, T0 - timedelta(hours=3), T0 - timedelta(minutes=20), far_venue)]
    f = rules.impossible_travel(sf, TH)
    assert f and "mph" in f.summary


def test_possible_travel_is_quiet():
    sf = shift()
    near_venue = (30.30, -97.7431)  # ~2.3 mi, under travel_min_miles
    sf.others = [OtherShift(2, "e2", "Next door", 2, T0 - timedelta(hours=3), T0 - timedelta(minutes=5), near_venue)]
    assert rules.impossible_travel(sf, TH) is None


# --- recap ---------------------------------------------------------------
def test_recap_before_clock_out():
    sf = shift(recap_submitted_at=T0 + timedelta(hours=1))
    f = rules.recap_before_clock_out(sf, TH)
    assert f and f.severity == rules.HIGH  # 3 h early


def test_recap_far_away():
    sf = shift(recap_ping_miles=12.0, recap_ping_minutes=4.0)
    assert rules.recap_far_away(sf, TH) is not None
    sf.recap_ping_minutes = 40.0  # ping too far from the submit to judge
    assert rules.recap_far_away(sf, TH) is None


def test_no_recap_only_after_shift():
    sf = shift(has_recap=False, recap_submitted_at=None)
    assert rules.no_recap(sf, TH) is not None
    sf.shift_over = False
    assert rules.no_recap(sf, TH) is None


# --- punch provenance ---------------------------------------------------------
def test_edited_punch_names_editor():
    sf = shift(punches=[punch("clock_in", T0), punch("clock_out", T0 + timedelta(hours=4), uby=99)])
    sf.editors = {99: "Ops Person"}
    f = rules.edited_punch(sf, TH)
    assert f and "Ops Person" in f.facts[0][1]


def test_edits_by_the_ba_themself_are_not_flagged():
    sf = shift(punches=[punch("clock_in", T0, uby=50), punch("clock_out", T0 + timedelta(hours=4))])
    assert rules.edited_punch(sf, TH) is None


def test_duplicate_punches():
    sf = shift(punches=[
        punch("clock_in", T0), punch("clock_out", T0 + timedelta(hours=1)),
        punch("clock_in", T0 + timedelta(hours=2)), punch("clock_out", T0 + timedelta(hours=4)),
    ])
    assert rules.duplicate_punches(sf, TH) is not None


def test_round_times():
    first_in = T0.replace(second=0)
    out = first_in + timedelta(hours=4)
    sf = shift(first_in=first_in, last_out=out, punches=[punch("clock_in", first_in), punch("clock_out", out)])
    assert rules.round_times(sf, TH) is not None
    assert rules.round_times(shift(), TH) is None  # :07 seconds = a real tap


def test_long_shift():
    f = rules.long_shift(shift(hours=11, worked=11), TH)
    assert f and f.severity == rules.MEDIUM
    assert rules.long_shift(shift(hours=15, worked=15), TH).severity == rules.HIGH


def test_mileage_mismatch():
    sf = shift(claim_miles=40.0, gps_miles=20.0)
    assert rules.mileage_mismatch(sf, TH) is not None
    sf.claim_miles = 23.0  # within 25% / 3 mi
    assert rules.mileage_mismatch(sf, TH) is None


def test_thresholds_override(settings):
    from hours_check.thresholds import thresholds

    settings.HOURS_CHECK_THRESHOLDS = {"far_miles": "0.05", "bogus": 1, "late_minutes": "x"}
    th = thresholds()
    assert th["far_miles"] == pytest.approx(0.05)
    assert th["late_minutes"] == DEFAULTS["late_minutes"]
    assert "bogus" not in th
    sf = shift(punches=[punch("clock_in", T0, NEAR), punch("clock_out", T0 + timedelta(hours=4), NEAR)])
    assert rules.far_clock_in(sf, th) is not None


def test_a_broken_rule_never_hides_the_others(monkeypatch):
    def boom(sf, th):
        raise RuntimeError("bad row")

    monkeypatch.setattr(rules, "ALL_RULES", (boom, rules.over_booked))
    assert [f.code for f in rules.check_shift(shift(worked=6), TH)] == ["over_booked"]


def test_explain_is_wording_only(settings, monkeypatch):
    """OpenAI only rewords: no key means no call; with a key it gets the
    rule's facts (no names) and its text never changes the flag."""
    from hours_check import explain

    flag = rules.over_booked(shift(worked=6), TH)
    before = (flag.code, flag.severity, flag.summary, flag.fingerprint)

    settings.OPENAI_API_KEY = ""
    assert explain.explain([flag]) == {}

    seen = {}

    def fake(system, user, **kw):
        seen["user"] = user
        return "  The shift ran two hours past its booking.  "

    import utils.ai_text as ai

    monkeypatch.setattr(ai, "generate_summary", fake)
    settings.OPENAI_API_KEY = "sk-test"
    from django.core.cache import cache

    cache.clear()
    out = explain.explain([flag])
    assert out[id(flag)] == "The shift ran two hours past its booking."
    assert "Ana" not in seen["user"] and "HEB" not in seen["user"]
    assert (flag.code, flag.severity, flag.summary, flag.fingerprint) == before
