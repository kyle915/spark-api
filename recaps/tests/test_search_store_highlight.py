"""Plain-English list search, the store profile, and the highlight of the
month.

Runs through the real ``schema_clients`` with the exact field sets the web
client asks for. The model helper is mocked; the live API is never called.
"""

from __future__ import annotations

from datetime import date, datetime, time
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.models import Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from events import models as event_models
from recaps import models as recap_models
from recaps.feedback_themes import Comment
from recaps.list_search import SEARCHES_PER_HOUR, normalize_filter
from recaps.month_highlight import (
    MIN_MONTH_RECAPS,
    MIN_STORE_CONSUMERS,
    parse_month,
    pick_quote,
    pick_store,
)
from recaps.store_places import valid_key
from utils.ai_text import AiUnavailable

GENERATE = "utils.ai_text.generate_json"
PUBLIC_URL = "recaps.photo_gallery.public_url"

# The exact field sets the web client asks for.
SEARCH_Q = """
query ListSearchQuery(
  $tenantId: ID!
  $list: String!
  $question: String
  $filter: ListSearchFilterInput
) {
  listSearch(tenantId: $tenantId, list: $list, question: $question, filter: $filter) {
    list
    interpreted
    modelAvailable
    ignored
    reason
    total
    filter {
      state
      city
      store
      dateFrom
      dateTo
      status
      activationType
      product
      conditions { metric op value }
    }
    rows {
      kind
      uuid
      title
      date
      store
      city
      state
      activationType
      status
      requestUuid
      recapCount
      consumers
      unitsSold
      conversion
    }
  }
}
"""
PROFILE_Q = """
query StoreProfileQuery($tenantId: ID!, $storeKey: String!) {
  storeProfile(tenantId: $tenantId, storeKey: $storeKey) {
    storeKey
    name
    address
    city
    state
    summary
    summarySource
    visitTotal
    totals {
      visits
      upcoming
      approvedRecaps
      consumers
      unitsSold
      conversion
      averageConsumers
      averageUnitsSold
      firstVisit
      lastVisit
    }
    visits {
      eventUuid
      requestUuid
      title
      date
      activationType
      status
      recapUuid
      recapKind
      consumers
      unitsSold
      conversion
    }
    products { label samples sold }
    comments { text kind recapUuid recapKind }
    photos { url category recapUuid eventDate }
    staff {
      inReview
      people { name visits lastDate }
      flags { recapUuid recapKind date labels }
    }
  }
}
"""
RECAP_STORE_Q = """
query RecapStoreKeyQuery($tenantId: ID!, $recapId: ID!) {
  recapStoreKey(tenantId: $tenantId, recapId: $recapId)
}
"""
STORE_LINKS_Q = """
query StoreLinksQuery($tenantId: ID!) {
  tenantStoreLinks(tenantId: $tenantId) { name storeKey }
}
"""
HIGHLIGHT_Q = """
query MonthHighlightQuery($tenantId: ID!, $month: String!) {
  monthHighlight(tenantId: $tenantId, month: $month) {
    month
    label
    caption
    captionSource
    recaps
    store {
      storeKey
      name
      market
      conversion
      consumers
      unitsSold
      recaps
      recapUuid
      recapKind
    }
    quote { text recapUuid recapKind }
    photo { url category recapUuid }
  }
}
"""

OCTOBER = {
    "state": "TX",
    "city": None,
    "store": "Kroger",
    "dateFrom": "2026-10-01",
    "dateTo": "2026-10-31",
    "status": None,
    "activationType": None,
    "product": None,
    "conditions": [{"metric": "conversion", "op": "lt", "value": 10}],
    "unsupported": [],
}
SENTENCE = "Texas Kroger events in October with conversion under 10%"


def _reworded(suffix=""):
    """A model that returns each draft unchanged (plus ``suffix``)."""

    def _fake(system, user, **kwargs):
        items = []
        for block in user.split("\n\nITEM "):
            block = block.removeprefix("ITEM ")
            key = block.split("\n", 1)[0].strip()
            draft = block.rsplit("DRAFT: ", 1)[-1].strip()
            items.append({"key": key, "text": draft + suffix})
        return {"items": items}

    return _fake


# ─── Pure rules ─────────────────────────────────────────────────────


def test_the_filter_keeps_only_what_checks_out():
    flt, ignored = normalize_filter(
        {
            "state": "Texass",
            "city": "  Houston ",
            "store": "Kroger",
            "dateFrom": "next month",
            "dateTo": "2026-10-31",
            "status": "everything",
            "activationType": "retail",
            "product": None,
            "ambassador": "Jordan Rivera",
            "conditions": [
                {"metric": "conversion", "op": "lt", "value": 10},
                {"metric": "cost per sample", "op": "lt", "value": 2},
                {"metric": "consumers", "op": "between", "value": 5},
                {"metric": "units sold", "op": "gte", "value": "many"},
            ],
            "unsupported": ["worked by Jordan"],
        },
        list_name="recaps",
        staff=True,
    )
    assert flt["state"] is None
    assert flt["city"] == "Houston"
    assert flt["date_from"] is None and flt["date_to"] == date(2026, 10, 31)
    assert flt["status"] is None
    assert flt["activation_type"] == "retail"
    assert flt["conditions"] == [{"metric": "conversion", "op": "lt", "value": 10.0}]
    assert "ambassador" not in flt
    said = " ".join(ignored)
    assert "Texass" in said and "next month" in said and "everything" in said
    assert "cost per sample" in said
    assert "worked by Jordan" in said


def test_a_state_name_becomes_its_code_and_dates_are_put_in_order():
    flt, ignored = normalize_filter(
        {"state": "texas", "dateFrom": "2026-10-31", "dateTo": "2026-10-01"},
        list_name="events",
        staff=False,
    )
    assert flt["state"] == "TX"
    assert (flt["date_from"], flt["date_to"]) == (date(2026, 10, 1), date(2026, 10, 31))
    assert ignored == []


def test_a_brand_user_is_always_on_approved_recaps():
    flt, ignored = normalize_filter(
        {"status": "needs_review", "state": "TX"}, list_name="recaps", staff=False
    )
    assert flt["status"] == "approved"
    assert ignored == ["Only approved recaps are shown here."]


def test_a_numeric_condition_moves_staff_to_approved_recaps():
    flt, ignored = normalize_filter(
        {
            "status": "needs_review",
            "conditions": [{"metric": "consumers", "op": "gt", "value": 50}],
        },
        list_name="recaps",
        staff=True,
    )
    assert flt["status"] == "approved"
    assert any("approved recaps only" in line for line in ignored)


def test_recap_status_is_not_an_events_filter():
    flt, ignored = normalize_filter(
        {"status": "approved", "state": "TX"}, list_name="events", staff=True
    )
    assert flt["status"] is None
    assert any("recaps list" in line for line in ignored)


def test_a_store_key_has_one_shape():
    assert valid_key(" s42 ") == "s42"
    for bad in ("42", "s", "s4x", "s1 OR 1=1", "", None, "S42"):
        assert valid_key(bad) is None


def test_month_parsing():
    assert parse_month("2026-10") == (date(2026, 10, 1), date(2026, 10, 31))
    assert parse_month("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    for bad in ("2026-13", "october", "2026-10-01", "", None):
        assert parse_month(bad) is None


def _fact(event_id, consumers, units, base=None, recap_id=None):
    return {
        "kind": "custom",
        "id": recap_id or event_id,
        "event_id": event_id,
        "consumers": consumers,
        "units": units,
        "base": consumers if base is None else base,
    }


def test_best_store_is_the_highest_conversion_over_the_floor():
    store_of = {1: "s1", 2: "s1", 3: "s2", 4: "s3", 5: "s4"}
    facts = [
        _fact(1, 50, 5),
        _fact(2, 50, 5),  # s1: 10%
        _fact(3, 40, 16),  # s2: 40%
        _fact(4, MIN_STORE_CONSUMERS - 1, 29),  # s3: nearly 100%, under the floor
        _fact(5, 500, 400, base=0),  # s4: no conversion base
        _fact(6, 100, 100),  # no store: counts for none
    ]
    best = pick_store(facts, store_of)
    assert best["key"] == "s2" and best["conversion"] == 40.0
    assert best["top"]["id"] == 3


def test_a_conversion_tie_goes_to_more_units_sold():
    best = pick_store(
        [_fact(1, 50, 10), _fact(2, 100, 20)], {1: "s1", 2: "s2"}
    )
    assert best["key"] == "s2"


def test_no_store_over_the_floor_is_no_best_store():
    assert pick_store([_fact(1, 10, 9)], {1: "s1"}) is None


def test_the_quote_is_the_longest_praise_that_is_not_a_complaint():
    long_complaint = Comment(
        "C1", "general", "Loved the can but it was far too sweet for me and too expensive."
    )
    short = Comment("C2", "general", "Loved it.")
    decline = Comment(
        "C3", "decline", "Loved the taste and would buy it again if the store had it."
    )
    good = Comment(
        "C4", "general", "I loved the citrus flavor and bought two cases for the weekend."
    )
    longer = Comment(
        "C5",
        "general",
        "This is my favorite sparkling water now, I enjoyed every flavor I tried today.",
    )
    assert pick_quote([long_complaint, short, decline, good, longer]) is longer
    assert pick_quote([long_complaint, short, decline]) is None


def test_stored_liked_themes_choose_the_quote_when_they_exist():
    plain = Comment(
        "C1", "general", "Shoppers kept coming back to the table for a second sample."
    )
    praise = Comment(
        "C2", "general", "I loved the citrus flavor and bought two cases for the weekend."
    )
    assert pick_quote([plain, praise]) is praise
    assert pick_quote([plain, praise], ["Shoppers kept coming back to the table…"]) is plain


# ─── Through the database and the real schema ───────────────────────


@pytest.mark.django_db(transaction=True)
class TestSearchStoreHighlight(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Search Brand")
        self.other = self.create_tenant(name="Search Other")
        self.kit = {t.id: self._kit(t) for t in (self.tenant, self.other)}
        self.ca = event_models.State.objects.create(
            name="California", code="CA", created_by=self.sys
        )
        self.tx = event_models.State.objects.create(
            name="Texas", code="TX", created_by=self.sys
        )
        self.kroger = self._retailer("Kroger")
        self.costco = self._retailer("Costco")
        self.admin = self.create_user(
            username="search-admin", email="search-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="search-client", email="search-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        credited = self.create_user(
            username="jordan-rivera-search", email="jordan.rivera.search@ba.test",
            role=self.roles["ambassador"],
        )
        credited.first_name, credited.last_name = "Jordan", "Rivera"
        credited.save()
        self.ba = self.create_ambassador(credited)
        self.file_type = recap_models.FileType.objects.create(
            name="image", created_by=self.sys
        )
        self.sampling = recap_models.FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=self.sys
        )
        self.receipts = recap_models.FileRecapCategory.objects.create(
            name="Receipts", tenant=self.tenant, created_by=self.sys
        )
        self.houston = self._store(self.kroger, "100 Texas Ave, Houston, TX 77001", self.tx)
        self.dallas = self._store(self.kroger, "200 Elm St, Dallas, TX 75201", self.tx)
        self.la = self._store(self.costco, "1 Main St, Los Angeles, CA 90001", self.ca)

    # ── fixtures ──────────────────────────────────────────────────────

    def _kit(self, tenant):
        et = event_models.EventType.objects.create(
            name="Retail Sampling", slug=f"search-retail-{tenant.id}", tenant=tenant,
            created_by=self.sys,
        )
        template = recap_models.CustomRecapTemplate.objects.create(
            name="Search Retail", event_type=et, tenant=tenant, created_by=self.sys,
        )
        num = recap_models.CustomRecapFieldType.objects.create(
            name=f"Number {tenant.id}", created_by=self.sys
        )
        section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=tenant, created_by=self.sys
        )

        def field(name):
            return recap_models.CustomField.objects.create(
                name=name, custom_recap_template=template, custom_field_type=num,
                recap_section=section, created_by=self.sys,
            )

        return {
            "event_type": et,
            "template": template,
            "request_type": event_models.RequestType.objects.create(
                name="Retail", tenant=tenant, created_by=self.sys
            ),
            "sampled": field("Total number of consumers sampled"),
            "sold": field("Cans sold"),
            "feedback": field("Consumer feedback"),
            "notes": field("Notes"),
        }

    def _retailer(self, name, tenant=None):
        return event_models.Retailer.objects.create(
            name=name, tenant=tenant or self.tenant, created_by=self.sys
        )

    def _store(self, retailer, address, state, tenant=None, deleted=False):
        tenant = tenant or self.tenant
        return event_models.Request.objects.create(
            name=f"{retailer.name} demo", address=address, tenant=tenant,
            retailer=retailer, state=state,
            request_type=self.kit[tenant.id]["request_type"],
            created_by=self.sys,
            deleted_at=timezone.now() if deleted else None,
        )

    def _recap(
        self,
        day,
        sampled,
        sold=0,
        *,
        store,
        approved=True,
        archived=False,
        feedback=None,
        notes=None,
        photo=None,
    ):
        tenant = store.tenant
        kit = self.kit[tenant.id]
        when = timezone.make_aware(datetime.combine(day, time(18)))
        event = event_models.Event.objects.create(
            name=f"{store.retailer.name} {day.isoformat()}", tenant=tenant,
            address=store.address, date=when, event_type=kit["event_type"],
            retailer=store.retailer, state=store.state, request=store,
            created_by=self.sys, updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=approved, submitted_at=when, event=event,
            tenant=tenant, custom_recap_template=kit["template"],
            ambassador=self.ba, created_by=self.sys,
            archived_at=timezone.now() if archived else None,
        )
        values = [(kit["sampled"], sampled), (kit["sold"], sold)]
        if feedback:
            values.append((kit["feedback"], feedback))
        if notes:
            values.append((kit["notes"], notes))
        for field, value in values:
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=field, value=str(value),
                created_by=self.sys,
            )
        for name, category in photo or ():
            recap_models.CustomRecapFile.objects.create(
                name=name, url=f"recap_files/{name}", custom_recap=recap,
                file_type=self.file_type, file_recap_category=category,
                created_by=self.sys,
            )
        return recap

    def _seed(self):
        """October 2026. Kroger Houston: 3 recaps at 5%. Kroger Dallas: 2 at
        40%. Costco LA: 2 at 20%. Plus rows that must never count."""
        oct_ = lambda d: date(2026, 10, d)  # noqa: E731
        self.h = [
            self._recap(
                oct_(5 + i), 100, 5, store=self.houston,
                feedback=(
                    "I loved the citrus flavor and bought two cases for the weekend. "
                    "Jordan Rivera was great."
                    if i == 0
                    else None
                ),
                notes="Jordan Rivera restocked the table twice." if i == 0 else None,
            )
            for i in range(3)
        ]
        self.d = [
            self._recap(
                oct_(8 + i), 100, 40, store=self.dallas,
                photo=(
                    [("receipt.jpg", self.receipts), ("table.jpg", self.sampling)]
                    if i == 0
                    else None
                ),
            )
            for i in range(2)
        ]
        self.l = [self._recap(oct_(10 + i), 100, 20, store=self.la) for i in range(2)]
        self.november = self._recap(date(2026, 11, 3), 100, 50, store=self.houston)
        # None of these may count, or reach the model or a brand user.
        self.unapproved = self._recap(
            oct_(12), 7777, 7000, store=self.houston, approved=False,
            feedback="I loved every single can and bought the whole shelf, truly.",
            photo=[("unapproved.jpg", self.sampling)],
        )
        self.archived = self._recap(oct_(13), 8888, 1, store=self.houston, archived=True)
        gone = self._store(
            self.kroger, "100 Texas Ave, Houston, TX 77001", self.tx, deleted=True
        )
        self._recap(oct_(14), 9999, 1, store=gone)
        theirs = self._store(
            self._retailer("Kroger", self.other),
            "100 Texas Ave, Houston, TX 77001",
            self.tx,
            tenant=self.other,
        )
        self.other_store = theirs
        self._recap(oct_(6), 6666, 1, store=theirs)

    def _vars(self, tenant=None, **kw):
        return {"tenantId": str((tenant or self.tenant).id), **kw}

    async def _run(self, query, variables, user):
        res = await self._execute_mutation(
            query, variables, self.endpoint_path, user=user
        )
        assert res.errors is None, res.errors
        return res.data

    async def _search(self, user, list_name="events", tenant=None, **kw):
        data = await self._run(
            SEARCH_Q, self._vars(tenant, list=list_name, **kw), user
        )
        return data["listSearch"]

    # ── search ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_a_sentence_becomes_a_filter_and_the_orm_runs_it(self):
        await sync_to_async(self._seed)()
        with patch(GENERATE, return_value=dict(OCTOBER)) as generate:
            out = await self._search(self.client_user, question=SENTENCE)
        assert out["interpreted"] is True and out["modelAvailable"] is True
        assert out["filter"]["state"] == "TX" and out["filter"]["store"] == "Kroger"
        assert out["filter"]["conditions"] == [
            {"metric": "conversion", "op": "lt", "value": 10.0}
        ]
        # Houston's three October events at 5%; Dallas (40%) and LA are out,
        # November is out, and the unapproved recap's event has no figures.
        assert out["total"] == 3
        assert {r["date"] for r in out["rows"]} == {"2026-10-05", "2026-10-06", "2026-10-07"}
        assert all(
            (r["city"], r["state"], r["conversion"], r["consumers"], r["unitsSold"])
            == ("Houston", "TX", 5.0, 100, 5)
            for r in out["rows"]
        )
        # The model was given names of places and products, never people,
        # and no figure from any recap.
        prompt = generate.call_args[0][1]
        assert "Kroger" in prompt and "TX" in prompt and SENTENCE in prompt
        for hidden in ("Rivera", "Jordan", "7777", "6666"):
            assert hidden not in prompt

    @pytest.mark.asyncio
    async def test_the_recaps_list_reads_the_same_filter(self):
        await sync_to_async(self._seed)()
        with patch(GENERATE, return_value=dict(OCTOBER)):
            out = await self._search(self.client_user, "recaps", question=SENTENCE)
        assert out["filter"]["status"] == "approved"
        assert sorted(r["uuid"] for r in out["rows"]) == sorted(str(r.uuid) for r in self.h)
        assert {r["status"] for r in out["rows"]} == {"approved"}
        assert {r["kind"] for r in out["rows"]} == {"custom"}

    @pytest.mark.asyncio
    async def test_model_output_is_not_trusted_and_dropped_parts_are_named(self):
        await sync_to_async(self._seed)()
        wild = {
            **OCTOBER,
            "state": "Texarkana",
            "status": "needs_review",
            "tenantId": str(self.other.id),
            "conditions": [
                {"metric": "cost per sample", "op": "lt", "value": 2},
                {"metric": "conversion", "op": "lt", "value": 10},
            ],
            "unsupported": ["staffed by Jordan"],
        }
        with patch(GENERATE, return_value=wild):
            out = await self._search(self.client_user, "recaps", question="anything")
        said = " ".join(out["ignored"])
        assert "Texarkana" in said and "cost per sample" in said
        assert "staffed by Jordan" in said and "Only approved recaps" in said
        assert out["filter"]["state"] is None and out["filter"]["status"] == "approved"
        # Still the brand's own approved Kroger recaps under 10%.
        assert sorted(r["uuid"] for r in out["rows"]) == sorted(str(r.uuid) for r in self.h)

    @pytest.mark.asyncio
    async def test_a_filter_built_by_hand_never_calls_the_model(self):
        await sync_to_async(self._seed)()
        with patch(GENERATE) as generate:
            out = await self._search(
                self.client_user,
                "recaps",
                question=SENTENCE,
                filter={
                    "store": "costco",
                    "dateFrom": "2026-10-01",
                    "dateTo": "2026-10-31",
                    "conditions": [{"metric": "units_sold", "op": "gte", "value": 20}],
                },
            )
        generate.assert_not_called()
        assert out["interpreted"] is False
        assert sorted(r["uuid"] for r in out["rows"]) == sorted(str(r.uuid) for r in self.l)

    @pytest.mark.asyncio
    async def test_with_the_model_unavailable_the_filter_still_works_by_hand(self):
        await sync_to_async(self._seed)()
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            unread = await self._search(self.client_user, question=SENTENCE)
            by_hand = await self._search(
                self.client_user, filter={"city": "Dallas"}
            )
        assert unread["modelAvailable"] is False and unread["rows"] == []
        assert unread["interpreted"] is False
        assert by_hand["total"] == 2
        assert {r["city"] for r in by_hand["rows"]} == {"Dallas"}

    @pytest.mark.asyncio
    async def test_a_brand_user_sees_approved_recaps_and_staff_may_see_the_rest(self):
        await sync_to_async(self._seed)()
        flt = {"city": "Houston", "dateFrom": "2026-10-01", "dateTo": "2026-10-31"}
        with patch(GENERATE) as generate:
            mine = await self._search(
                self.client_user, "recaps", filter={**flt, "status": "needs_review"}
            )
            review = await self._search(
                self.admin, "recaps", filter={**flt, "status": "needs_review"}
            )
            parked = await self._search(
                self.admin, "recaps", filter={**flt, "status": "archived"}
            )
        generate.assert_not_called()
        assert sorted(r["uuid"] for r in mine["rows"]) == sorted(str(r.uuid) for r in self.h)
        assert [r["uuid"] for r in review["rows"]] == [str(self.unapproved.uuid)]
        assert review["rows"][0]["status"] == "needs_review"
        # No figure is shown for a recap that is not approved.
        assert review["rows"][0]["consumers"] is None
        assert [r["uuid"] for r in parked["rows"]] == [str(self.archived.uuid)]

    @pytest.mark.asyncio
    async def test_a_brand_user_is_held_to_their_own_brand_and_staff_may_choose(self):
        await sync_to_async(self._seed)()
        flt = {"state": "TX"}
        mine = await self._search(self.client_user, "recaps", self.other, filter=flt)
        theirs = await self._search(self.admin, "recaps", self.other, filter=flt)
        assert mine["total"] == 6  # their own Texas recaps, not the other brand's
        assert theirs["total"] == 1
        assert theirs["rows"][0]["consumers"] == 6666

    @pytest.mark.asyncio
    async def test_a_sentence_read_once_is_not_read_again_and_people_are_limited(self):
        await sync_to_async(self._seed)()
        with patch(GENERATE, return_value=dict(OCTOBER)) as generate:
            await self._search(self.client_user, question=SENTENCE)
            await self._search(self.client_user, question=f"  {SENTENCE.upper()} ")
            assert generate.call_count == 1

            def _fill():
                for i in range(SEARCHES_PER_HOUR):
                    recap_models.DashboardText.objects.create(
                        tenant=self.tenant, kind="list_search", scope_key=f"k{i}",
                        inputs_hash="x", payload={}, created_by=self.client_user,
                    )

            await sync_to_async(_fill)()
            limited = await self._search(self.client_user, question="something new")
            assert generate.call_count == 1
            # A stored reading and a hand filter are not limited.
            again = await self._search(self.client_user, question=SENTENCE)
            by_hand = await self._search(self.client_user, filter={"city": "Dallas"})
        assert "lot of searches" in limited["reason"] and limited["rows"] == []
        assert again["total"] == 3 and by_hand["total"] == 2

    @pytest.mark.asyncio
    async def test_nothing_asked_for_is_not_everything(self):
        await sync_to_async(self._seed)()
        empty = {**OCTOBER, "state": None, "store": None, "dateFrom": None,
                 "dateTo": None, "conditions": []}
        with patch(GENERATE, return_value=empty):
            out = await self._search(self.client_user, question="hello there")
        assert out["rows"] == [] and out["total"] == 0
        assert out["reason"] == "Nothing in that could be used as a filter."

    # ── store profile ─────────────────────────────────────────────────

    def _key(self, request):
        from recaps.store_places import tenant_places

        return tenant_places(request.tenant_id).index.store_for_request(request.pk)

    async def _profile(self, user, key, tenant=None):
        data = await self._run(PROFILE_Q, self._vars(tenant, storeKey=key), user)
        return data["storeProfile"]

    @pytest.mark.asyncio
    async def test_a_store_page_adds_up_its_approved_recaps(self):
        await sync_to_async(self._seed)()
        key = await sync_to_async(self._key)(self.houston)
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            page = await self._profile(self.client_user, key)
        assert (page["name"], page["city"], page["state"]) == ("Kroger", "Houston", "TX")
        totals = page["totals"]
        # Three October recaps and one in November: 400 sampled, 65 sold.
        assert totals["approvedRecaps"] == 4
        assert (totals["consumers"], totals["unitsSold"]) == (400, 65)
        assert totals["conversion"] == 16.2
        assert totals["averageConsumers"] == 100.0
        assert page["summarySource"] == "template"
        assert page["summary"] == (
            "Kroger has 4 approved recaps between October 5, 2026 and November 3, 2026. "
            "Those recaps recorded 400 consumers sampled and 65 units sold, "
            "a conversion rate of 16.2%."
        )
        for hidden in ("7,777", "7777", "8,888", "9,999", "6,666"):
            assert hidden not in page["summary"]

    @pytest.mark.asyncio
    async def test_a_brand_user_gets_no_names_no_review_state_and_no_staff_block(self):
        await sync_to_async(self._seed)()
        key = await sync_to_async(self._key)(self.houston)

        def _clock():
            source, _ = Source.objects.get_or_create(name="clock_in")
            Attendance.objects.create(
                clock_time=self.h[0].event.date, ambassador=self.ba, job=None,
                event=self.h[0].event, source=source,
            )

        await sync_to_async(_clock)()
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            brand = await self._profile(self.client_user, key)
            staff = await self._profile(self.admin, key)

        assert brand["staff"] is None
        assert "Rivera" not in str(brand) and "Jordan" not in str(brand)
        statuses = {v["date"]: v["status"] for v in brand["visits"]}
        # The event whose recap is still in review reads as no approved recap.
        assert statuses["2026-10-12"] == "no_approved_recap"
        assert statuses["2026-10-05"] == "recap_approved"
        assert all(v["recapUuid"] != str(self.unapproved.uuid) for v in brand["visits"])
        # The comment is the consumer's, with the ambassador's name taken
        # out; the Notes field and the unapproved recap's comment are absent.
        assert [c["text"] for c in brand["comments"]] == [
            "I loved the citrus flavor and bought two cases for the weekend. "
            "the brand ambassador was great."
        ]
        assert brand["comments"][0]["recapUuid"] == str(self.h[0].uuid)
        assert brand["photos"] == []

        assert staff["staff"]["inReview"] == 1
        assert staff["staff"]["people"][0]["name"] == "Jordan Rivera"
        in_review = {v["date"]: v for v in staff["visits"]}["2026-10-12"]
        assert in_review["status"] == "recap_in_review"
        assert in_review["recapUuid"] == str(self.unapproved.uuid)
        # Same figures for both.
        assert staff["totals"] == brand["totals"] and staff["summary"] == brand["summary"]

    @pytest.mark.asyncio
    async def test_photos_are_approved_recaps_pictures_and_never_receipts(
        self, monkeypatch
    ):
        monkeypatch.setattr(PUBLIC_URL, lambda blob: f"https://cdn.test/{blob}")
        await sync_to_async(self._seed)()
        key = await sync_to_async(self._key)(self.dallas)
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            page = await self._profile(self.client_user, key)
        assert [p["category"] for p in page["photos"]] == ["Sampling photos"]
        assert page["photos"][0]["url"].endswith("table.jpg")
        assert page["photos"][0]["recapUuid"] == str(self.d[0].uuid)

    @pytest.mark.asyncio
    async def test_a_store_key_cannot_cross_brands(self):
        await sync_to_async(self._seed)()
        theirs = await sync_to_async(self._key)(self.other_store)
        mine = await sync_to_async(self._key)(self.houston)
        assert theirs != mine
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            # The brand user asks for the other brand's store, with either
            # tenant id: they are held to their own brand, where it is no store.
            assert await self._profile(self.client_user, theirs) is None
            assert await self._profile(self.client_user, theirs, self.other) is None
            # Staff reading the other brand do get it, and only its recap.
            page = await self._profile(self.admin, theirs, self.other)
            assert page["totals"]["consumers"] == 6666
            assert await self._profile(self.admin, mine, self.other) is None
            assert await self._profile(self.client_user, "s1; drop") is None

    @pytest.mark.asyncio
    async def test_the_summary_is_reworded_only_when_every_number_checks_out(self):
        await sync_to_async(self._seed)()
        key = await sync_to_async(self._key)(self.houston)
        with patch(GENERATE, side_effect=_reworded(" Nice.")) as generate:
            first = await self._profile(self.client_user, key)
            again = await self._profile(self.admin, key)
            assert generate.call_count == 1
        assert first["summarySource"] == "model" and first["summary"].endswith("Nice.")
        assert again["summary"] == first["summary"]
        prompt = generate.call_args[0][1]
        for hidden in ("7,777", "8,888", "9,999", "6,666", "Rivera", "Jordan"):
            assert hidden not in prompt

    @pytest.mark.asyncio
    async def test_a_number_the_model_made_up_falls_back_to_the_built_summary(self):
        await sync_to_async(self._seed)()
        key = await sync_to_async(self._key)(self.houston)

        def _wrong(system, user, **kwargs):
            return {"items": [{"key": "summary", "text": "Kroger sampled 4,000 people."}]}

        with patch(GENERATE, side_effect=_wrong):
            page = await self._profile(self.client_user, key)
        assert page["summarySource"] == "template"
        assert "4,000" not in page["summary"] and "400 consumers" in page["summary"]

    @pytest.mark.asyncio
    async def test_a_store_with_no_approved_recap_has_no_summary_and_asks_nobody(self):
        def _only_review():
            return self._recap(
                date(2026, 10, 12), 50, 5, store=self.houston, approved=False
            )

        await sync_to_async(_only_review)()
        key = await sync_to_async(self._key)(self.houston)
        with patch(GENERATE) as generate:
            page = await self._profile(self.client_user, key)
        generate.assert_not_called()
        assert (page["summary"], page["summarySource"]) == ("", "none")
        assert page["totals"]["approvedRecaps"] == 0 and page["totals"]["conversion"] is None

    @pytest.mark.asyncio
    async def test_links_to_a_store_from_a_recap_and_from_a_chart_row(self):
        await sync_to_async(self._seed)()
        key = await sync_to_async(self._key)(self.la)
        ok = await self._run(
            RECAP_STORE_Q, self._vars(recapId=str(self.l[0].uuid)), self.client_user
        )
        assert ok["recapStoreKey"] == key
        # A recap the brand may not see has no store link.
        hidden = await self._run(
            RECAP_STORE_Q, self._vars(recapId=str(self.unapproved.uuid)), self.client_user
        )
        assert hidden["recapStoreKey"] is None
        staff = await self._run(
            RECAP_STORE_Q, self._vars(recapId=str(self.unapproved.uuid)), self.admin
        )
        assert staff["recapStoreKey"] == await sync_to_async(self._key)(self.houston)
        links = await self._run(STORE_LINKS_Q, self._vars(), self.client_user)
        # Kroger is two stores, so its chart row links nowhere; Costco is one.
        assert links["tenantStoreLinks"] == [{"name": "Costco", "storeKey": key}]

    # ── highlight of the month ────────────────────────────────────────

    async def _highlight(self, user, month="2026-10", tenant=None):
        data = await self._run(HIGHLIGHT_Q, self._vars(tenant, month=month), user)
        return data["monthHighlight"]

    @pytest.mark.asyncio
    async def test_the_highlight_is_picked_by_rule(self, monkeypatch):
        monkeypatch.setattr(PUBLIC_URL, lambda blob: f"https://cdn.test/{blob}")
        await sync_to_async(self._seed)()
        dallas = await sync_to_async(self._key)(self.dallas)
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            brand = await self._highlight(self.client_user)
            staff = await self._highlight(self.admin)
        assert brand == staff
        assert (brand["label"], brand["recaps"]) == ("October 2026", 7)
        store = brand["store"]
        assert (store["storeKey"], store["name"], store["market"]) == (
            dallas, "Kroger", "Dallas, TX",
        )
        assert (store["conversion"], store["consumers"], store["unitsSold"]) == (40.0, 200, 80)
        assert store["recapUuid"] in {str(r.uuid) for r in self.d}
        assert brand["captionSource"] == "template"
        assert brand["caption"] == (
            "Kroger in Dallas, TX had the highest conversion rate in October 2026: "
            "40% from 200 consumers sampled, with 80 units sold."
        )
        # The stored comment, with the ambassador's name taken out; never
        # the unapproved recap's longer one.
        assert brand["quote"]["text"].startswith("I loved the citrus flavor")
        assert "Rivera" not in str(brand) and "whole shelf" not in str(brand)
        assert brand["quote"]["recapUuid"] == str(self.h[0].uuid)
        # The best store's sampling photo, not its receipt.
        assert brand["photo"]["url"].endswith("table.jpg")
        assert brand["photo"]["recapUuid"] == str(self.d[0].uuid)

    @pytest.mark.asyncio
    async def test_the_caption_falls_back_when_the_model_adds_a_number(self):
        await sync_to_async(self._seed)()

        def _wrong(system, user, **kwargs):
            return {"items": [{"key": "caption", "text": "Dallas doubled to 80% conversion."}]}

        with patch(GENERATE, side_effect=_wrong):
            out = await self._highlight(self.client_user)
        assert out["captionSource"] == "template" and "80%" not in out["caption"]
        with patch(GENERATE) as generate:
            again = await self._highlight(self.client_user)
        # Stored: a second load asks nobody.
        generate.assert_not_called()
        assert again["caption"] == out["caption"]

    @pytest.mark.asyncio
    async def test_the_caption_may_be_reworded_with_the_same_numbers(self):
        await sync_to_async(self._seed)()
        with patch(GENERATE, side_effect=_reworded(" Well done.")) as generate:
            out = await self._highlight(self.client_user)
        assert out["captionSource"] == "model" and out["caption"].endswith("Well done.")
        prompt = generate.call_args[0][1]
        for hidden in ("7,777", "8,888", "9,999", "6,666", "Rivera", "citrus"):
            assert hidden not in prompt

    @pytest.mark.asyncio
    async def test_a_thin_month_shows_nothing_and_asks_nobody(self):
        def _thin():
            for i in range(MIN_MONTH_RECAPS - 1):
                self._recap(date(2026, 9, 1 + i), 100, 40, store=self.dallas)

        await sync_to_async(_thin)()
        with patch(GENERATE) as generate:
            assert await self._highlight(self.client_user, "2026-09") is None
            assert await self._highlight(self.client_user, "2026-08") is None
            assert await self._highlight(self.client_user, "not-a-month") is None
        generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_store_over_the_floor_and_no_quote_shows_nothing(self):
        def _small():
            for i in range(MIN_MONTH_RECAPS):
                self._recap(
                    date(2026, 9, 1 + i), 5, 4,
                    store=(self.dallas, self.houston, self.la)[i % 3],
                )

        await sync_to_async(_small)()
        assert MIN_STORE_CONSUMERS > 10
        with patch(GENERATE) as generate:
            assert await self._highlight(self.client_user, "2026-09") is None
        generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_highlight_is_the_callers_own_brand(self):
        await sync_to_async(self._seed)()
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            mine = await self._highlight(self.client_user, tenant=self.other)
            theirs = await self._highlight(self.admin, tenant=self.other)
        assert mine["store"]["name"] == "Kroger" and mine["recaps"] == 7
        assert theirs is None  # one recap: under the floor
