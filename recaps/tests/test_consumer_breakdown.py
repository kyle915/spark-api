"""Consumers sampled by week, store and state: every split adds up."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from events import models as event_models
from recaps import models as recap_models
from recaps.consumer_breakdown import (
    consumer_rows,
    place_from_walkin_title,
    summarize_consumers,
    week_start,
)


# ─── Pure roll-up ───────────────────────────────────────────────────


def _row(consumers, day, store="Costco", state="CA"):
    return {"consumers": consumers, "day": day, "store": store, "state": state}


def test_week_starts_on_monday():
    assert week_start(date(2026, 10, 7)) == date(2026, 10, 5)  # a Wednesday
    assert week_start(date(2026, 10, 5)) == date(2026, 10, 5)
    assert week_start(date(2026, 10, 11)) == date(2026, 10, 5)  # a Sunday


def test_every_split_adds_up_to_the_total():
    out = summarize_consumers(
        [
            _row(100, date(2026, 10, 5)),
            _row(40, date(2026, 10, 6), store="Kroger", state="tx"),
            _row(25, date(2026, 10, 20), store="", state=""),
            _row(10, date(2026, 10, 21), store="costco "),
        ]
    )
    assert out["total"] == 175 and out["recaps"] == 4
    for split in ("by_week", "by_store", "by_state"):
        assert sum(r["consumers"] for r in out[split]) == 175, split


def test_weeks_are_contiguous_and_cover_the_window():
    out = summarize_consumers(
        [_row(100, date(2026, 10, 5)), _row(25, date(2026, 10, 20))],
        start=date(2026, 9, 30),
        end=date(2026, 10, 28),
    )
    assert [w["week_start"] for w in out["by_week"]] == [
        "2026-09-28",
        "2026-10-05",
        "2026-10-12",
        "2026-10-19",
        "2026-10-26",
    ]
    assert [w["consumers"] for w in out["by_week"]] == [0, 100, 0, 25, 0]


def test_missing_store_is_kept_named_honestly_and_sorted_last():
    out = summarize_consumers(
        [
            # The unrecorded pile is the biggest: it still must not lead.
            _row(900, date(2026, 10, 5), store=None, state=None),
            _row(100, date(2026, 10, 5), store="Costco"),
            _row(50, date(2026, 10, 5), store="COSTCO"),
            _row(60, date(2026, 10, 5), store="Kroger", state="TX"),
        ]
    )
    stores = out["by_store"]
    assert [(r["label"], r["kind"], r["consumers"]) for r in stores] == [
        ("Costco", "named", 150),
        ("Kroger", "named", 60),
        ("", "missing", 900),
    ]
    assert out["store_count"] == 2
    assert out["by_state"][-1]["kind"] == "missing"
    assert out["state_count"] == 2


def test_long_store_list_folds_its_tail():
    rows = [_row(100 - i, date(2026, 10, 5), store=f"Store {i:02d}") for i in range(12)]
    out = summarize_consumers(rows, top_stores=10)
    stores = out["by_store"]
    assert len(stores) == 11
    assert stores[-1]["kind"] == "other"
    assert stores[-1]["consumers"] == (100 - 10) + (100 - 11)
    assert stores[-1]["recaps"] == 2
    assert out["store_count"] == 12
    assert sum(r["consumers"] for r in stores) == out["total"]


def test_empty_window():
    out = summarize_consumers([], start=date(2026, 10, 1), end=date(2026, 10, 31))
    assert out["total"] == 0 and out["recaps"] == 0
    assert out["by_week"] == [] and out["by_store"] == [] and out["by_state"] == []


# ─── Walk-in titles: the place when no retailer resolves ────────────


@pytest.mark.parametrize(
    "title,address,event_type,expected",
    [
        (
            "10/19/2026 - Lockhart City Park, Lockhart, Texas (BBQ Fest) · Event Activation",
            "Lockhart City Park, Lockhart, Texas",
            "Event Activation",
            ("BBQ Fest", "venue"),
        ),
        # The program suffix alone marks it, when the event type is unknown.
        (
            "11/2/2026 - 600 2nd St N, St. Petersburg, FL (Savor St Pete) · Event Activation",
            "600 2nd St N, St. Petersburg, FL",
            None,
            ("Savor St Pete", "venue"),
        ),
        # A retail program: the typed store is a named store, not a venue.
        (
            "11/5/2026 - 1200 Main St, Austin, TX 78701 (Sunoco) · Retail Sampling",
            "1200 Main St, Austin, TX 78701",
            "Retail Sampling",
            ("Sunoco", "named"),
        ),
        # A " · " inside the parenthetical is not the program suffix.
        (
            "8/21/2026 - 1825 S Campus Dr, Salt Lake City, UT 84112, USA "
            "(Huntsman Center (TONY HAWK VERT ALERT) · 2026-08-22 (2/2)) · Event Activation",
            "1825 S Campus Dr, Salt Lake City, UT 84112, USA",
            "Event Activation",
            ("Huntsman Center (TONY HAWK VERT ALERT)", "venue"),
        ),
        # No parenthetical: the title names the venue directly.
        (
            "8/16/2026 - Wisconsin State Fair · Event Activation",
            "",
            "Event Activation",
            ("Wisconsin State Fair", "venue"),
        ),
        # Only the street address: nothing to name.
        (
            "8/14/2026 - 7859 Harlem Avenue, Burbank, IL 60459 · Retail Sampling",
            "7859 Harlem Avenue, Burbank, IL 60459",
            "Retail Sampling",
            ("", ""),
        ),
        ("9/4/2026 - Product Seeding · BA 769 · 2026-09-04", "", None, ("", "")),
        # Not a walk-in title: left alone.
        ("LAFC Event", "Los Angeles, CA", "Event Activation", ("", "")),
        ("", "", None, ("", "")),
    ],
)
def test_place_from_walkin_title(title, address, event_type, expected):
    assert place_from_walkin_title(title, address, event_type) == expected


def test_venue_rows_rank_with_named_rows_but_stay_distinguishable():
    out = summarize_consumers(
        [
            {**_row(5000, date(2026, 10, 19), store="BBQ Fest"), "store_kind": "venue"},
            {**_row(850, date(2026, 11, 2), store="Savor St Pete"), "store_kind": "venue"},
            # Case-insensitive grouping within venues.
            {**_row(50, date(2026, 11, 3), store="savor st pete"), "store_kind": "venue"},
            _row(900, date(2026, 10, 20), store="Costco"),
            _row(28, date(2026, 11, 5), store="Sunoco"),
            _row(70, date(2026, 11, 6), store=None),
        ]
    )
    assert [(r["label"], r["kind"], r["consumers"]) for r in out["by_store"]] == [
        ("BBQ Fest", "venue", 5000),
        ("Costco", "named", 900),
        ("Savor St Pete", "venue", 900),
        ("Sunoco", "named", 28),
        ("", "missing", 70),
    ]
    assert out["store_count"] == 4
    assert sum(r["consumers"] for r in out["by_store"]) == out["total"] == 6898


def test_a_venue_and_a_store_of_the_same_name_are_two_rows():
    out = summarize_consumers(
        [
            {**_row(10, date(2026, 10, 5), store="Main Street"), "store_kind": "venue"},
            _row(20, date(2026, 10, 5), store="Main Street"),
        ]
    )
    assert sorted((r["label"], r["kind"]) for r in out["by_store"]) == [
        ("Main Street", "named"),
        ("Main Street", "venue"),
    ]


# ─── Through the database and the real schema ───────────────────────

# The full field set the web client asks for (ConsumerBreakdownQuery).
BREAKDOWN_Q = """
query B($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantConsumerBreakdown(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    total
    recaps
    storeCount
    stateCount
    byWeek { weekStart consumers recaps }
    byStore { label kind consumers recaps }
    byState { label kind consumers recaps }
  }
}
"""
KPIS_Q = """
query K($tenantId: ID!, $year: Int) {
  tenantKpis(tenantId: $tenantId, year: $year) { consumersReached }
}
"""


@pytest.mark.django_db(transaction=True)
class TestConsumerBreakdownSchema(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Breakdown Brand")
        self.other = self.create_tenant(name="Breakdown Other")
        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="breakdown-retail", tenant=self.tenant,
            created_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Breakdown Retail", event_type=self.et, tenant=self.tenant,
            created_by=self.sys,
        )
        self.num = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.sys
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.sampled = recap_models.CustomField.objects.create(
            name="Total number of consumers sampled",
            custom_recap_template=self.template, custom_field_type=self.num,
            recap_section=self.section, created_by=self.sys,
        )
        self.ca = event_models.State.objects.create(
            name="California", code="CA", created_by=self.sys
        )
        self.costco = event_models.Retailer.objects.create(
            name="Costco", tenant=self.tenant, created_by=self.sys
        )
        self.request_type = event_models.RequestType.objects.create(
            name="Retail", tenant=self.tenant, created_by=self.sys
        )

    def _event(self, *, month=10, day, tenant=None, **kwargs):
        when = timezone.make_aware(datetime(2026, month, day, 18))
        return event_models.Event.objects.create(
            name=f"Event {month}-{day}", tenant=tenant or self.tenant,
            address="1 Main St", date=when, event_type=self.et,
            created_by=self.sys, updated_by=self.sys, **kwargs,
        )

    def _custom(self, event, sampled, *, approved=True, **kwargs):
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=approved, submitted_at=event.date, event=event,
            tenant=event.tenant, custom_recap_template=self.template,
            created_by=self.sys, **kwargs,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=self.sampled, value=str(sampled),
            created_by=self.sys,
        )
        return recap

    def _legacy(self, event, consumers, *, approved=True):
        recap = recap_models.Recap.objects.create(
            name="legacy", event=event, approved=approved,
            created_by=self.sys, updated_by=self.sys,
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=recap, total_consumer=consumers,
            created_by=self.sys, updated_by=self.sys,
        )
        return recap

    def _request(self, **kwargs):
        return event_models.Request.objects.create(
            name="Gig", address="1 Main St", request_type=self.request_type,
            tenant=self.tenant, created_by=self.sys,
            date=timezone.make_aware(datetime(2026, 10, 8, 18)), **kwargs,
        )

    def _client_user(self):
        user = self.create_user(
            username="c@breakdown.com", email="c@breakdown.com",
            role=self.roles["client"], password="password123",
        )
        self.create_tenanted_user(user=user, tenant=self.tenant)
        return user

    def _seed(self):
        # Counted: a custom recap at Costco in CA, week of Oct 5.
        self._custom(self._event(day=6, retailer=self.costco, state=self.ca), 400)
        # Counted: a legacy recap at the same store the week after.
        self._legacy(self._event(day=13, retailer=self.costco, state=self.ca), 100)
        # Counted, with no store and no state on the event.
        self._custom(self._event(day=14), 60)
        # Counted: the store only exists as text on the request.
        self._custom(
            self._event(day=15, state=self.ca, request=self._request(retailer_name="HEB")),
            30,
        )
        # Not counted: waiting for approval.
        self._custom(self._event(day=7, retailer=self.costco, state=self.ca), 999, approved=False)
        self._legacy(self._event(day=7, retailer=self.costco, state=self.ca), 999, approved=False)
        # Not counted: the gig was deleted.
        self._custom(
            self._event(
                day=8, retailer=self.costco, state=self.ca,
                request=self._request(deleted_at=timezone.now()),
            ),
            999,
        )
        # Not counted: an agency filing kept out of totals.
        self._custom(
            self._event(day=9, retailer=self.costco, state=self.ca),
            999, exclude_from_aggregates=True,
        )
        # Not counted: an event kept off the dashboard.
        self._custom(
            self._event(day=9, retailer=self.costco, state=self.ca, exclude_from_dashboard=True),
            999,
        )
        # Not counted: the event happened before the window.
        self._custom(self._event(month=8, day=3, retailer=self.costco, state=self.ca), 999)
        # Not counted: another brand.
        self._custom(self._event(day=6, tenant=self.other), 999)
        return self._client_user()

    def _walkin(self, title, address, *, day, event_type=None, **kwargs):
        when = timezone.make_aware(datetime(2026, 10, day, 18))
        return event_models.Event.objects.create(
            name=title, tenant=self.tenant, address=address, date=when,
            event_type=event_type or self.et, created_by=self.sys,
            updated_by=self.sys, **kwargs,
        )

    def _seed_walkins(self):
        activation = event_models.EventType.objects.create(
            name="Event Activation", slug="breakdown-activation", tenant=self.tenant,
            created_by=self.sys,
        )
        tx = event_models.State.objects.create(name="Texas", code="TX", created_by=self.sys)
        # A festival with no request and no retailer: a venue row.
        self._custom(
            self._walkin(
                "10/19/2026 - Lockhart City Park, Lockhart, Texas (BBQ Fest) · Event Activation",
                "Lockhart City Park, Lockhart, Texas",
                day=19, event_type=activation, state=tx,
            ),
            5000,
        )
        # Two recaps at one venue, typed in different case: one row.
        for sampled, name in ((800, "Savor St Pete"), (50, "SAVOR ST PETE")):
            self._custom(
                self._walkin(
                    f"10/20/2026 - 600 2nd St N, St. Petersburg, FL ({name}) · Event Activation",
                    "600 2nd St N, St. Petersburg, FL",
                    day=20, event_type=activation,
                ),
                sampled,
            )
        # A retail walk-in at a store the brand never typed: a named store.
        self._custom(
            self._walkin(
                "10/21/2026 - 1200 Main St, Austin, TX 78701 (Sunoco) · Retail Sampling",
                "1200 Main St, Austin, TX 78701",
                day=21,
            ),
            28,
        )
        # Address only: still "missing".
        self._custom(
            self._walkin(
                "10/22/2026 - 7859 Harlem Avenue, Burbank, IL 60459 · Retail Sampling",
                "7859 Harlem Avenue, Burbank, IL 60459",
                day=22,
            ),
            12,
        )
        # A retailer on the event still wins over the title.
        self._custom(
            self._walkin(
                "10/23/2026 - 1 Main St (Somewhere Else) · Event Activation",
                "1 Main St",
                day=23, event_type=activation, retailer=self.costco, state=self.ca,
            ),
            100,
        )
        return self._client_user()

    @pytest.mark.asyncio
    async def test_walkin_titles_name_venues_and_stores_and_totals_hold(self):
        me = await sync_to_async(self._seed_walkins)()
        res = await self._execute_mutation(
            BREAKDOWN_Q,
            {"tenantId": str(self.tenant.id), "startDate": "2026-10-01", "endDate": "2026-10-31"},
            self.endpoint_path,
            user=me,
        )
        assert res.errors is None, res.errors
        b = res.data["tenantConsumerBreakdown"]
        assert b["total"] == 5000 + 850 + 28 + 12 + 100
        for split in ("byWeek", "byStore", "byState"):
            assert sum(r["consumers"] for r in b[split]) == b["total"], split
        assert [(r["label"], r["kind"], r["consumers"]) for r in b["byStore"]] == [
            ("BBQ Fest", "venue", 5000),
            ("Savor St Pete", "venue", 850),
            ("Costco", "named", 100),
            ("Sunoco", "named", 28),
            ("", "missing", 12),
        ]
        assert b["storeCount"] == 4

    def _seed_legacy_own_place(self):
        tx = event_models.State.objects.create(name="Texas", code="TX", created_by=self.sys)
        kroger = event_models.Retailer.objects.create(
            name="Kroger", tenant=self.tenant, created_by=self.sys
        )
        # The event knows nothing; the legacy recap names its own store and state.
        recap = self._legacy(self._event(day=6), 70)
        recap.retailer = kroger
        recap.state = tx
        recap.save(update_fields=["retailer", "state"])
        # The recap's own retailer wins over the event's; the event's state
        # stays (the recap's only fills in), as on the custom path.
        recap = self._legacy(self._event(day=7, retailer=self.costco, state=self.ca), 30)
        recap.retailer = kroger
        recap.state = tx
        recap.save(update_fields=["retailer", "state"])
        # Nothing on the recap: the event's store and state as before.
        self._legacy(self._event(day=8, retailer=self.costco, state=self.ca), 5)

    @pytest.mark.django_db(transaction=True)
    def test_legacy_recap_prefers_its_own_retailer_and_fills_in_its_state(self):
        self._seed_legacy_own_place()
        rows = consumer_rows(self.tenant.id, date(2026, 10, 1), date(2026, 10, 31))
        got = sorted((r["consumers"], r["store"], r["state"]) for r in rows)
        assert got == [(5, "Costco", "CA"), (30, "Kroger", "CA"), (70, "Kroger", "TX")]
        out = summarize_consumers(rows)
        assert out["total"] == 105
        assert [(r["label"], r["consumers"]) for r in out["by_store"]] == [
            ("Kroger", 100),
            ("Costco", 5),
        ]

    @pytest.mark.asyncio
    async def test_splits_add_up_and_leave_out_what_kpis_leave_out(self):
        me = await sync_to_async(self._seed)()
        res = await self._execute_mutation(
            BREAKDOWN_Q,
            {"tenantId": str(self.tenant.id), "startDate": "2026-10-01", "endDate": "2026-10-31"},
            self.endpoint_path,
            user=me,
        )
        assert res.errors is None, res.errors
        b = res.data["tenantConsumerBreakdown"]
        assert b["total"] == 590
        assert b["recaps"] == 4
        for split in ("byWeek", "byStore", "byState"):
            assert sum(r["consumers"] for r in b[split]) == 590, split

        weeks = {w["weekStart"]: w["consumers"] for w in b["byWeek"]}
        assert weeks == {
            "2026-09-28": 0,
            "2026-10-05": 400,
            "2026-10-12": 190,
            "2026-10-19": 0,
            "2026-10-26": 0,
        }
        assert [(r["label"], r["kind"], r["consumers"]) for r in b["byStore"]] == [
            ("Costco", "named", 500),
            ("HEB", "named", 30),
            ("", "missing", 60),
        ]
        assert [(r["label"], r["kind"], r["consumers"]) for r in b["byState"]] == [
            ("CA", "named", 530),
            ("", "missing", 60),
        ]
        assert b["storeCount"] == 2 and b["stateCount"] == 1

    @pytest.mark.asyncio
    async def test_agrees_with_the_kpi_total_for_the_same_year(self):
        me = await sync_to_async(self._seed)()
        variables = {"tenantId": str(self.tenant.id)}
        res = await self._execute_mutation(
            BREAKDOWN_Q,
            {**variables, "startDate": "2026-01-01", "endDate": "2026-12-31"},
            self.endpoint_path,
            user=me,
        )
        kpis = await self._execute_mutation(
            KPIS_Q, {**variables, "year": 2026}, self.endpoint_path, user=me
        )
        assert res.errors is None and kpis.errors is None
        # The August recap is inside the year, so both count it.
        assert res.data["tenantConsumerBreakdown"]["total"] == 590 + 999
        assert (
            res.data["tenantConsumerBreakdown"]["total"]
            == kpis.data["tenantKpis"]["consumersReached"]
        )

    @pytest.mark.asyncio
    async def test_a_client_cannot_read_another_brand(self):
        me = await sync_to_async(self._seed)()
        res = await self._execute_mutation(
            BREAKDOWN_Q, {"tenantId": str(self.other.id)}, self.endpoint_path, user=me
        )
        assert res.errors is None, res.errors
        b = res.data["tenantConsumerBreakdown"]
        # Either refused outright or pinned back to their own brand: never
        # the other brand's 999.
        assert b is None or b["total"] != 999


def test_the_sql_gate_lets_through_every_wording_the_matcher_counts():
    """The gate filters field names in SQL before the matcher runs, so it must
    accept every wording the consumers-sampled matcher accepts. One it missed
    ("...consumers did you sample?") dropped a whole template version's
    consumers from the totals."""
    from recaps.tenant_overview import _CUSTOM_KPI_NAME_RE
    from recaps.types import _CONSUMERS_SAMPLED_RE

    wordings = [
        "Total number of consumers sampled",
        "How many TOTAL consumers did you sample?",
        "How many consumers did you interact with?",
        "Consumer sampled",
    ]
    for name in wordings:
        assert _CONSUMERS_SAMPLED_RE.search(name), name
        assert _CUSTOM_KPI_NAME_RE.search(name), name


def test_chart_questions_name_venues_and_keep_missing_aside(monkeypatch):
    from recaps import chart_questions, consumer_breakdown

    monkeypatch.setattr(
        consumer_breakdown,
        "tenant_consumer_breakdown",
        lambda tenant_id, start, end: summarize_consumers(
            [
                {**_row(5000, date(2026, 10, 19), store="BBQ Fest"), "store_kind": "venue"},
                _row(28, date(2026, 11, 5), store="Sunoco"),
                _row(70, date(2026, 11, 6), store=None),
            ]
        ),
    )
    data = chart_questions.chart_data(1, chart_questions.CHART_STORE, None, None)
    assert data.rows == [("BBQ Fest (event venue)", 5000), ("Sunoco", 28)]
    assert data.asides == [("No store recorded", 70)]
