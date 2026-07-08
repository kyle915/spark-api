"""Coverage for recaps/field_sampling_report.py — the consolidated Field
Sampling Report backend (samples/hour, YTD + weekly SKU breakdowns,
locations hit, upcoming shifts, the deterministic call-outs feed, and the
on-demand Gemini narrative over them). Feel Free's Guerrilla Field Sampling
program is the first — and so far only — consumer.

Every shared KPI/date-window primitive here is proven elsewhere
(tenant_overview, events.pnl) and reused unmodified — these tests focus on
what's NEW: the "<Market> — <Corridor> · <date>" name parser, the
quantity/sessions/none SKU-breakdown fallback ladder, and the market/
event-type scoping applied consistently across all six sub-reports.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

import pytest

from ambassadors.models import AmbassadorEvent, Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.field_sampling_report import (
    MAX_CALLOUTS,
    _parse_event_name,
    build_field_sampling_report,
    field_callouts,
    generate_ai_callout_summary,
    locations_hit,
    samples_per_hour,
    sku_breakdown,
    upcoming_shifts,
)


class TestParseEventName:
    def test_market_and_corridor(self):
        assert _parse_event_name("Miami — Wynwood · 9/24") == ("Miami", "Wynwood")

    def test_corridor_without_trailing_date(self):
        assert _parse_event_name("Austin — South Congress") == (
            "Austin",
            "South Congress",
        )

    def test_no_em_dash_returns_none_none(self):
        assert _parse_event_name("Vons Sparks") == (None, None)

    def test_blank_returns_none_none(self):
        assert _parse_event_name("") == (None, None)
        assert _parse_event_name(None) == (None, None)


WHEN = datetime(2026, 6, 10, 18, 0, tzinfo=_tz.utc)  # inside the window below
WINDOW_START = datetime(2026, 6, 8, 0, 0, tzinfo=_tz.utc)
WINDOW_END = datetime(2026, 6, 15, 0, 0, tzinfo=_tz.utc)
OUTSIDE_WHEN = datetime(2026, 1, 5, 18, 0, tzinfo=_tz.utc)


@pytest.mark.django_db(transaction=True)
class FieldSamplingReportTestBase(AmbassadorsGraphQLTestCase):
    """Shared fixture helpers for every TestCase below."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Feel Free")
        self.sampling_type = self.create_event_type(
            name="Field Sampling", tenant=self.tenant
        )
        self.other_type = self.create_event_type(
            name="Retail Sampling", tenant=self.tenant
        )
        self.admin = self.create_user(
            username="admin-fsr",
            email="admin-fsr@test.com",
            role=self.roles["spark_admin"],
        )

    def _event(self, name, *, event_type=None, when=WHEN, **kwargs):
        return self.create_event(
            name=name,
            tenant=self.tenant,
            event_type=event_type or self.sampling_type,
            date=when,
            start_time=kwargs.pop("start_time", when),
            **kwargs,
        )

    def _product(self, name):
        product_type = event_models.ProductType.objects.create(
            name="Beverage", tenant=self.tenant, created_by=self.system_user
        )
        return event_models.Product.objects.create(
            name=name,
            product_type=product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _template(self, name):
        event_type = self.create_event_type(name=f"ET {name}", tenant=self.tenant)
        return recap_models.CustomRecapTemplate.objects.create(
            name=name,
            event_type=event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )

    def _custom_recap(self, event, template):
        return recap_models.CustomRecap.objects.create(
            name=f"recap for {event.name}",
            event=event,
            tenant=self.tenant,
            custom_recap_template=template,
            created_by=self.system_user,
        )

    def _product_sample(self, custom_recap, product, quantity):
        return recap_models.CustomRecapProductSample.objects.create(
            custom_recap=custom_recap,
            product=product,
            quantity=quantity,
            created_by=self.system_user,
        )

    def _field_value(self, custom_recap, field_name, value):
        ft, _ = recap_models.CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.system_user}
        )
        section = recap_models.RecapSection.objects.create(
            tenant=self.tenant, name="Field Section", created_by=self.system_user
        )
        field = recap_models.CustomField.objects.create(
            custom_recap_template=custom_recap.custom_recap_template,
            recap_section=section,
            name=field_name,
            custom_field_type=ft,
            created_by=self.system_user,
        )
        return recap_models.CustomFieldValue.objects.create(
            custom_recap=custom_recap,
            custom_field=field,
            value=value,
            created_by=self.system_user,
        )

    def _staff(self, event, *, hours=None, with_clocks=True):
        """Book an ambassador on ``event``; optionally clock a real pair."""
        user = self.create_user(
            username=f"ba-{event.id}",
            email=f"ba-{event.id}@test.com",
            role=self.roles["ambassador"],
        )
        ambassador = self.create_ambassador(user)
        AmbassadorEvent.objects.create(
            ambassador=ambassador,
            event=event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.admin,
        )
        if with_clocks:
            in_src, _ = Source.objects.get_or_create(name="clock_in")
            out_src, _ = Source.objects.get_or_create(name="clock_out")
            start = event.start_time
            Attendance.objects.create(
                clock_time=start,
                coordinates=None,
                ambassador=ambassador,
                job=None,
                event=event,
                source=in_src,
            )
            Attendance.objects.create(
                clock_time=start + timedelta(hours=hours or 4),
                coordinates=None,
                ambassador=ambassador,
                job=None,
                event=event,
                source=out_src,
            )
        return ambassador


class TestSkuBreakdown(FieldSamplingReportTestBase):
    def test_quantity_mode_sums_across_events(self):
        template = self._template("Quantity Tmpl")
        e1 = self._event("Miami — Wynwood · 6/10")
        e2 = self._event("Austin — South Congress · 6/11", when=WHEN + timedelta(days=1))
        cola = self._product("Cola")
        lemon = self._product("Lemon Lime")
        r1 = self._custom_recap(e1, template)
        r2 = self._custom_recap(e2, template)
        self._product_sample(r1, cola, 30)
        self._product_sample(r1, lemon, 10)
        self._product_sample(r2, cola, 20)

        result = sku_breakdown(self.tenant.id, WINDOW_START, WINDOW_END)
        assert result["mode"] == "quantity"
        by_product = {i["product"]: i["total"] for i in result["items"]}
        assert by_product == {"Cola": 50, "Lemon Lime": 10}
        # Sorted by descending total.
        assert result["items"][0]["product"] == "Cola"

    def test_sessions_mode_falls_back_when_no_structured_rows(self):
        template = self._template("Choice Tmpl")
        e1 = self._event("Miami — Wynwood · 6/10")
        e2 = self._event("Miami — Brickell · 6/11", when=WHEN + timedelta(days=1))
        e3 = self._event("Miami — Design District · 6/12", when=WHEN + timedelta(days=2))
        r1 = self._custom_recap(e1, template)
        r2 = self._custom_recap(e2, template)
        r3 = self._custom_recap(e3, template)
        # "select" (single string) and "multiselect" (JSON array) shapes.
        self._field_value(r1, "Which products were sampled?", "Cola")
        self._field_value(r2, "Which products were sampled?", '["Cola", "Lemon Lime"]')
        self._field_value(r3, "Which products were sampled?", '["Lemon Lime"]')

        result = sku_breakdown(self.tenant.id, WINDOW_START, WINDOW_END)
        assert result["mode"] == "sessions"
        by_product = {i["product"]: i["total"] for i in result["items"]}
        # Cola: 2 distinct sessions (r1, r2). Lemon Lime: 2 distinct sessions (r2, r3).
        assert by_product == {"Cola": 2, "Lemon Lime": 2}

    def test_quantity_takes_precedence_over_sessions(self):
        template = self._template("Mixed Tmpl")
        e1 = self._event("Miami — Wynwood · 6/10")
        e2 = self._event("Miami — Brickell · 6/11", when=WHEN + timedelta(days=1))
        cola = self._product("Cola")
        r1 = self._custom_recap(e1, template)
        r2 = self._custom_recap(e2, template)
        self._product_sample(r1, cola, 15)
        self._field_value(r2, "Which products were sampled?", "Lemon Lime")

        result = sku_breakdown(self.tenant.id, WINDOW_START, WINDOW_END)
        assert result["mode"] == "quantity"
        assert result["items"] == [{"product": "Cola", "total": 15}]

    def test_none_mode_when_no_product_data(self):
        template = self._template("Empty Tmpl")
        e1 = self._event("Miami — Wynwood · 6/10")
        r1 = self._custom_recap(e1, template)
        self._field_value(r1, "General Notes", "Sunny day, good turnout")

        result = sku_breakdown(self.tenant.id, WINDOW_START, WINDOW_END)
        assert result == {"mode": "none", "items": []}

    def test_market_filter(self):
        template = self._template("Market Tmpl")
        miami = self._event("Miami — Wynwood · 6/10")
        austin = self._event("Austin — South Congress · 6/11", when=WHEN + timedelta(days=1))
        cola = self._product("Cola")
        tea = self._product("Tea")
        self._product_sample(self._custom_recap(miami, template), cola, 40)
        self._product_sample(self._custom_recap(austin, template), tea, 25)

        result = sku_breakdown(self.tenant.id, WINDOW_START, WINDOW_END, market="Miami")
        assert result["items"] == [{"product": "Cola", "total": 40}]

    def test_window_excludes_outside_dates(self):
        template = self._template("Window Tmpl")
        cola = self._product("Cola")
        outside_event = self._event("Miami — Wynwood · 1/5", when=OUTSIDE_WHEN)
        self._product_sample(self._custom_recap(outside_event, template), cola, 99)

        result = sku_breakdown(self.tenant.id, WINDOW_START, WINDOW_END)
        assert result == {"mode": "none", "items": []}


class TestSamplesPerHour(FieldSamplingReportTestBase):
    def _staffed_metro_event(self, name, *, when=WHEN, hours=4, with_clocks=True):
        template = self._template(f"Tmpl {name[:8]}")
        event = self._event(
            name, when=when, end_time=when + timedelta(hours=hours)
        )
        recap = self._custom_recap(event, template)
        self._field_value(recap, "Consumers Sampled", "50")
        self._staff(event, hours=hours, with_clocks=with_clocks)
        return event

    def test_real_clock_pair_computes_per_hour(self):
        self._staffed_metro_event("Miami — Wynwood · 6/10", hours=4)
        result = samples_per_hour(self.tenant.id, WINDOW_START, WINDOW_END)
        assert result["samples"] == 50
        assert result["hours"] == 4.0
        assert result["per_hour"] == 12.5
        assert result["estimated"] is False

    def test_no_clocks_falls_back_to_scheduled_and_flags_estimated(self):
        self._staffed_metro_event("Miami — Wynwood · 6/10", hours=4, with_clocks=False)
        result = samples_per_hour(self.tenant.id, WINDOW_START, WINDOW_END)
        assert result["hours"] == 4.0  # scheduled duration
        assert result["estimated"] is True

    def test_market_filter_scopes_both_samples_and_hours(self):
        self._staffed_metro_event("Miami — Wynwood · 6/10", hours=4)
        self._staffed_metro_event(
            "Austin — South Congress · 6/11", when=WHEN + timedelta(days=1), hours=6
        )
        result = samples_per_hour(
            self.tenant.id, WINDOW_START, WINDOW_END, market="Miami"
        )
        assert result["samples"] == 50
        assert result["hours"] == 4.0

    def test_zero_hours_gives_none_per_hour(self):
        # No qualifying events at all in-window -> zero samples, zero hours.
        result = samples_per_hour(self.tenant.id, WINDOW_START, WINDOW_END)
        assert result == {
            "samples": 0,
            "hours": 0.0,
            "per_hour": None,
            "estimated": False,
        }


class TestLocationsHit(FieldSamplingReportTestBase):
    def test_parses_and_sorts_oldest_first(self):
        self._event(
            "Miami — Brickell · 6/12", when=WHEN + timedelta(days=2), address="200 Brickell Ave"
        )
        self._event("Miami — Wynwood · 6/10", when=WHEN, address="100 Wynwood Way")

        rows = locations_hit(self.tenant.id, WINDOW_START, WINDOW_END)
        assert [r["corridor"] for r in rows] == ["Wynwood", "Brickell"]
        assert rows[0]["market"] == "Miami"
        assert rows[0]["address"] == "100 Wynwood Way"

    def test_skips_events_without_naming_convention(self):
        self._event("Vons Sparks")  # no em dash -> not a "stop"
        rows = locations_hit(self.tenant.id, WINDOW_START, WINDOW_END)
        assert rows == []

    def test_market_filter(self):
        self._event("Miami — Wynwood · 6/10")
        self._event("Austin — South Congress · 6/11", when=WHEN + timedelta(days=1))
        rows = locations_hit(self.tenant.id, WINDOW_START, WINDOW_END, market="Austin")
        assert len(rows) == 1
        assert rows[0]["market"] == "Austin"


class TestUpcomingShifts(FieldSamplingReportTestBase):
    def test_only_future_events_within_days_window(self):
        # Freeze "now" indirectly by picking events relative to timezone.now();
        # use a wide horizon and confirm inclusion/exclusion at the boundary.
        from django.utils import timezone as dj_tz

        now = dj_tz.now()
        soon = self._event(
            "Miami — Wynwood · soon", when=now + timedelta(days=2), address="1 Ocean Dr"
        )
        too_far = self._event(
            "Miami — Brickell · far", when=now + timedelta(days=30)
        )
        past = self._event("Miami — Edgewater · past", when=now - timedelta(days=1))

        result = upcoming_shifts(self.tenant.id, days=7)
        names = {i["name"] for i in result["items"]}
        assert soon.name in names
        assert too_far.name not in names
        assert past.name not in names
        assert result["total"] == len(result["items"]) == 1

    def test_market_filter(self):
        from django.utils import timezone as dj_tz

        now = dj_tz.now()
        self._event("Miami — Wynwood · soon", when=now + timedelta(days=1))
        self._event("Austin — South Congress · soon", when=now + timedelta(days=1))

        result = upcoming_shifts(self.tenant.id, market="Austin", days=7)
        assert result["total"] == 1
        assert result["items"][0]["market"] == "Austin"


class TestFieldCallouts(FieldSamplingReportTestBase):
    def test_matches_feedback_vocabulary_and_normalizes_whitespace(self):
        template = self._template("Callout Tmpl")
        event = self._event("Miami — Wynwood · 6/10")
        recap = self._custom_recap(event, template)
        self._field_value(recap, "Consumer quotes", "Loved   the\ncans!")
        self._field_value(recap, "Foot traffic per hour", "120")  # not feedback

        rows = field_callouts(self.tenant.id, WINDOW_START, WINDOW_END)
        texts = [r["text"] for r in rows]
        assert "Loved the cans!" in texts
        assert not any("120" in t for t in texts)
        assert rows[0]["market"] == "Miami"
        assert rows[0]["corridor"] == "Wynwood"

    def test_matches_notes_field_via_postgres_word_boundary_fix(self):
        # Regression: Postgres's `~*` does not honor Python's `\b`; the
        # module's `_FEEDBACK_NAME_RE_IREGEX` uses `\y` instead so a field
        # literally named "Notes"/"Field Notes" is not silently excluded.
        template = self._template("Notes Tmpl")
        event = self._event("Miami — Wynwood · 6/10")
        recap = self._custom_recap(event, template)
        self._field_value(recap, "Field Notes", "Ran out of samples by 2pm")
        self._field_value(recap, "Notes", "Great weather all day")
        # A field that merely CONTAINS "notes" as a substring of a longer
        # word must NOT match (word-boundary correctness, not substring).
        self._field_value(recap, "Annotestation", "should not match")

        rows = field_callouts(self.tenant.id, WINDOW_START, WINDOW_END)
        texts = [r["text"] for r in rows]
        assert "Ran out of samples by 2pm" in texts
        assert "Great weather all day" in texts
        assert "should not match" not in texts

    def test_dedup_case_insensitive(self):
        template = self._template("Dedup Tmpl")
        e1 = self._event("Miami — Wynwood · 6/10")
        e2 = self._event("Miami — Brickell · 6/11", when=WHEN + timedelta(days=1))
        self._field_value(self._custom_recap(e1, template), "Comment", "Great turnout")
        self._field_value(self._custom_recap(e2, template), "Comment", "great turnout")

        rows = field_callouts(self.tenant.id, WINDOW_START, WINDOW_END)
        assert len(rows) == 1

    def test_market_filter(self):
        template = self._template("Market Callout Tmpl")
        miami = self._event("Miami — Wynwood · 6/10")
        austin = self._event("Austin — South Congress · 6/11", when=WHEN + timedelta(days=1))
        self._field_value(self._custom_recap(miami, template), "Comment", "Miami note")
        self._field_value(self._custom_recap(austin, template), "Comment", "Austin note")

        rows = field_callouts(self.tenant.id, WINDOW_START, WINDOW_END, market="Austin")
        assert [r["text"] for r in rows] == ["Austin note"]

    def test_capped_and_newest_first(self):
        template = self._template("Cap Tmpl")
        for i in range(MAX_CALLOUTS + 3):
            event = self._event(
                f"Miami — Stop {i} · 6/{10 + i}", when=WHEN + timedelta(days=i)
            )
            self._field_value(
                self._custom_recap(event, template), "Comment", f"Note number {i}"
            )

        rows = field_callouts(self.tenant.id, WINDOW_START, WHEN + timedelta(days=MAX_CALLOUTS + 5))
        assert len(rows) == MAX_CALLOUTS
        # Newest-first: the highest-numbered (most recent) note leads.
        assert rows[0]["text"] == f"Note number {MAX_CALLOUTS + 2}"


class TestBuildFieldSamplingReport(FieldSamplingReportTestBase):
    def test_shape_has_all_six_sections(self):
        result = build_field_sampling_report(self.tenant.id, WINDOW_START, WINDOW_END)
        assert set(result) == {
            "samples_per_hour",
            "ytd_sku_breakdown",
            "week_sku_breakdown",
            "locations_hit",
            "upcoming",
            "callouts",
        }

    def test_ytd_is_independent_of_the_passed_in_window(self):
        # A recap dated back in January (outside WINDOW_START/END, but
        # inside the current calendar year) must still show up in the YTD
        # SKU breakdown even though it's excluded from week_sku_breakdown.
        from django.utils import timezone as dj_tz

        template = self._template("YTD Tmpl")
        cola = self._product("Cola")
        january = dj_tz.now().replace(
            month=1, day=10, hour=12, minute=0, second=0, microsecond=0
        )
        jan_event = self._event("Miami — Wynwood · 1/10", when=january)
        self._product_sample(self._custom_recap(jan_event, template), cola, 77)

        result = build_field_sampling_report(self.tenant.id, WINDOW_START, WINDOW_END)
        assert result["ytd_sku_breakdown"]["items"] == [{"product": "Cola", "total": 77}]
        assert result["week_sku_breakdown"] == {"mode": "none", "items": []}


class TestGenerateAiCalloutSummary:
    def test_no_callouts_returns_none_without_calling_gemini(self):
        with patch(
            "recaps.field_sampling_report.generate_gemini_text"
        ) as mock_gemini:
            result = generate_ai_callout_summary("Feel Free", [], {"samples": 0})
        assert result is None
        mock_gemini.assert_not_called()

    def test_calls_gemini_with_callouts_and_returns_its_text(self):
        callouts = [
            {"market": "Miami", "corridor": "Wynwood", "date": "2026-06-10", "text": "Great turnout"},
        ]
        with patch(
            "recaps.field_sampling_report.generate_gemini_text",
            return_value="Solid week overall.",
        ) as mock_gemini:
            result = generate_ai_callout_summary(
                "Feel Free", callouts, {"samples": 50, "hours": 4, "per_hour": 12.5}
            )
        assert result == "Solid week overall."
        mock_gemini.assert_called_once()
        prompt = mock_gemini.call_args.args[0]
        assert "Feel Free" in prompt
        assert "Great turnout" in prompt
