"""A pasted email or sheet row read into draft request fields.

Covers: fields the text does not hold stay blank and are listed as "could
not read" (including ones the model made up); a date with no year is the next
time it comes round and says so; times are read from the text as typed;
products and the store are matched against what the tenant really has; phone
numbers, email addresses and the signature never leave Spark; a brand user is
refused; and nothing is created, changed or stored by parsing.

No test reaches OpenAI: ``utils.ai_text._post_json`` is patched.
"""

from __future__ import annotations

import json
from datetime import date
from unittest import mock

import pytest
from asgiref.sync import sync_to_async

from events import models as em
from events import request_intake as ri
from events.models import RetailerAlias
from events.tests.base import EventsGraphQLTestCase

# src/api/mutations/ParseRequestDetailsMutation.ts
FE_PARSE_MUTATION = """
mutation ParseRequestDetailsMutation($tenantId: ID!, $text: String!) {
  parseRequestDetails(tenantId: $tenantId, text: $text) {
    ok
    message
    truncated
    drafts {
      date
      dateAsTyped
      yearAssumed
      dateWarning
      startTime
      startAsTyped
      endTime
      endAsTyped
      timeAssumed
      storeName
      matchedStoreName
      storeNumber
      streetAddress
      city
      state
      zip
      address
      notes
      products {
        uuid
        name
      }
      unmatchedProducts
      couldNotRead
    }
  }
}
"""

POST_JSON = "utils.ai_text._post_json"
TODAY = date(2026, 11, 15)

SHEET_ROWS = (
    "TX\tFriday\t11/20/2026\tHeb 725\t10a\t1p\t2701 E 7th St, Austin, TX 78702\t"
    "Check in with the beverage manager\tMango Chainsaw, Severed Lime\n"
    "TX\tSaturday\t12/5\tTotal Wine & More\t11:30 AM\t2:30 PM\t\t\tBerry It Alive"
)

EMAIL = """Hi team,

Can we get a sampling at Spec's on 1/9 from 4 to 7pm? It's the one at
2410 Smith St, Houston. Please sample the Mango Chainsaw.

Thanks,
Jordan Example
Regional Manager
jordan@example.com
(512) 555-0142
"""


def _reply(*requests: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"requests": list(requests)})}}]}


def _row(**kwargs) -> dict:
    base = {
        "date_text": None, "weekday_text": None, "month": None, "day": None, "year": None,
        "start_text": None, "end_text": None, "store_name": None, "street_address": None,
        "city": None, "state": None, "zip": None, "notes": None, "products": [],
    }
    base.update(kwargs)
    return base


ROW_ONE = _row(
    date_text="11/20/2026", weekday_text="Friday", month=11, day=20, year=2026,
    start_text="10a", end_text="1p", store_name="Heb 725",
    street_address="2701 E 7th St", city="Austin", state="TX", zip="78702",
    notes="Check in with the beverage manager",
    products=["Mango Chainsaw", "Severed Lime"],
)
ROW_TWO = _row(
    date_text="12/5", weekday_text="Saturday", month=12, day=5, year=None,
    start_text="11:30 AM", end_text="2:30 PM", store_name="Total Wine & More",
    products=["Berry It Alive"],
)


# ─── Pure parsing ───────────────────────────────────────────────────


def test_times_are_read_as_typed():
    assert ri.resolve_times("10a", "1p") == ("10:00", "13:00", False)
    assert ri.resolve_times("11:30 AM", "2:30 PM") == ("11:30", "14:30", False)
    assert ri.resolve_times("9:00 a.m.", "noon") == ("09:00", "12:00", False)
    assert ri.resolve_times("14:00", "17:30") == ("14:00", "17:30", False)
    assert ri.resolve_times("12am", "12pm") == ("00:00", "12:00", False)


def test_a_bare_time_is_only_filled_in_when_the_other_end_settles_it():
    # "10-1p" can only be 10 in the morning; "4 to 7pm" is the afternoon.
    assert ri.resolve_times("10", "1p") == ("10:00", "13:00", True)
    assert ri.resolve_times("4", "7pm") == ("16:00", "19:00", True)
    assert ri.resolve_times("10am", "2") == ("10:00", "14:00", True)
    assert ri.resolve_times("10:00", "14:00") == ("10:00", "14:00", True)
    # Nothing settles these: left blank, not guessed.
    assert ri.resolve_times("10", "2") == ("", "", False)
    assert ri.resolve_times("9", None) == ("", "", False)
    assert ri.resolve_times("morning", "afternoon") == ("", "", False)
    assert ri.resolve_times("25:00", "13pm") == ("", "", False)


def test_a_date_with_no_year_is_the_next_time_it_comes_round():
    assert ri.resolve_date(11, 20, 2026, today=TODAY) == (date(2026, 11, 20), False)
    assert ri.resolve_date(11, 20, 26, today=TODAY) == (date(2026, 11, 20), False)
    assert ri.resolve_date(12, 5, None, today=TODAY) == (date(2026, 12, 5), True)
    assert ri.resolve_date(11, 15, None, today=TODAY) == (date(2026, 11, 15), True)  # today
    assert ri.resolve_date(1, 9, None, today=TODAY) == (date(2027, 1, 9), True)
    assert ri.resolve_date(2, 29, None, today=TODAY) == (date(2028, 2, 29), True)
    # Not a date: left blank.
    assert ri.resolve_date(2, 30, 2027, today=TODAY) == (None, False)
    assert ri.resolve_date(None, 5, None, today=TODAY) == (None, False)
    assert ri.resolve_date(13, 5, None, today=TODAY) == (None, False)
    assert ri.resolve_date(5, 5, 1999, today=TODAY) == (None, False)


def test_signature_phone_and_email_are_removed_before_sending():
    cleaned, truncated = ri.clean_pasted_text(EMAIL)
    assert truncated is False
    assert "Spec's on 1/9" in cleaned and "2410 Smith St" in cleaned
    for private in ("Jordan", "Regional Manager", "example.com", "555", "Thanks"):
        assert private not in cleaned


def test_a_row_pasted_under_a_sign_off_is_kept():
    text = "Two more for next week\nThanks!\n11/20\tHEB\t10a\t1p\t2701 E 7th St, Austin, TX 78702"
    cleaned, _ = ri.clean_pasted_text(text)
    assert "2701 E 7th St" in cleaned
    # A store called Best Buy is not a sign-off.
    cleaned, _ = ri.clean_pasted_text("Visit request\nBest Buy\n100 Main St, Austin, TX 78701")
    assert "Best Buy" in cleaned and "100 Main St" in cleaned


def test_long_text_is_cut():
    cleaned, truncated = ri.clean_pasted_text("HEB 10a 1p " * 3000)
    assert truncated is True and len(cleaned) == ri.MAX_TEXT_CHARS


def test_products_match_the_catalog_or_are_shown_unmatched():
    catalog = [("u1", "Mango Chainsaw 19.2oz"), ("u2", "Severed Lime"), ("u3", "Berry It Alive"), ("u4", "Berry Sparkling")]
    matched, unmatched = ri.match_products(
        ["mango chainsaw", "Severed  Lime", "Berry", "Grim Leafer", "severed lime"], catalog
    )
    assert matched == [("u1", "Mango Chainsaw 19.2oz"), ("u2", "Severed Lime")]
    # "Berry" fits two products, "Grim Leafer" fits none: neither is picked.
    assert unmatched == ["Berry", "Grim Leafer"]


def test_the_store_is_matched_to_a_known_retailer_or_alias():
    retailers = ["H-E-B", "Total Wine"]
    aliases = {"total wine & more": "Total Wine"}
    assert ri.match_store("heb", retailers, aliases) == ("H-E-B", "")
    assert ri.match_store("Heb 725", retailers, aliases) == ("H-E-B", "725")
    assert ri.match_store("Total Wine & More", retailers, aliases) == ("Total Wine", "")
    assert ri.match_store("Corner Bottle Shop", retailers, aliases) == ("", "")


# ─── Through the database and the real schema ───────────────────────


@pytest.mark.django_db(transaction=True)
class TestRequestIntake(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.OPENAI_API_KEY = "test-key"
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Intake Brand")
        self.other = self.create_tenant(name="Intake Other Brand")
        self.admin = self.create_user(
            username="intake-admin",
            email="intake-admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.brand_user = self.create_user(
            username="intake-client", email="intake-client@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.brand_user, self.tenant)
        ptype = em.ProductType.objects.create(name="Cans", tenant=self.tenant, created_by=self.sys)
        self.products = {
            name: em.Product.objects.create(
                name=name, product_type=ptype, tenant=self.tenant, created_by=self.sys
            )
            for name in ("Mango Chainsaw", "Severed Lime", "Berry It Alive")
        }
        other_type = em.ProductType.objects.create(name="Cans", tenant=self.other, created_by=self.sys)
        em.Product.objects.create(
            name="Grim Leafer", product_type=other_type, tenant=self.other, created_by=self.sys
        )
        em.Retailer.objects.create(name="H-E-B", tenant=self.tenant, created_by=self.sys)
        em.Retailer.objects.create(name="Total Wine", tenant=self.tenant, created_by=self.sys)
        RetailerAlias.objects.create(
            tenant=self.tenant, alias_key="total wine & more", alias_name="Total Wine & More",
            canonical_name="Total Wine", status="accepted",
        )

    def _counts(self):
        return (
            em.Request.objects.count(),
            em.Event.objects.count(),
            em.Retailer.objects.count(),
            em.Product.objects.count(),
            RetailerAlias.objects.count(),
        )

    async def _parse(self, text, user=None, tenant=None):
        return await self._execute_mutation(
            FE_PARSE_MUTATION,
            {"tenantId": str((tenant or self.tenant).id), "text": text},
            user=user or self.admin,
        )

    @pytest.mark.asyncio
    async def test_sheet_rows_become_a_queue_of_checked_drafts(self):
        before = await sync_to_async(self._counts)()
        with mock.patch(POST_JSON, return_value=_reply(ROW_ONE, ROW_TWO)) as post, mock.patch(
            "events.live_board._ops_today", return_value=TODAY
        ):
            res = await self._parse(SHEET_ROWS)
            assert post.call_count == 1
        assert res.errors is None, res.errors
        out = res.data["parseRequestDetails"]
        assert (out["ok"], out["message"], out["truncated"]) == (True, "", False)
        first, second = out["drafts"]

        assert first["date"] == "2026-11-20" and first["dateAsTyped"] == "11/20/2026"
        assert first["yearAssumed"] is False and first["dateWarning"] == ""
        assert (first["startTime"], first["startAsTyped"]) == ("10:00", "10a")
        assert (first["endTime"], first["endAsTyped"]) == ("13:00", "1p")
        assert first["timeAssumed"] is False
        # Matched in code to the retailer Spark already has; the number is kept.
        assert (first["storeName"], first["matchedStoreName"], first["storeNumber"]) == (
            "Heb 725", "H-E-B", "725",
        )
        assert first["address"] == "2701 E 7th St, Austin, TX 78702"
        assert first["notes"] == "Check in with the beverage manager"
        assert [p["name"] for p in first["products"]] == ["Mango Chainsaw", "Severed Lime"]
        assert first["products"][0]["uuid"] == str(self.products["Mango Chainsaw"].uuid)
        assert first["unmatchedProducts"] == [] and first["couldNotRead"] == []

        # No year written: the next 5 December, and the draft says so.
        assert second["date"] == "2026-12-05" and second["dateAsTyped"] == "12/5"
        assert second["yearAssumed"] is True
        assert (second["startTime"], second["endTime"]) == ("11:30", "14:30")
        # An accepted store-name alias is honoured.
        assert second["matchedStoreName"] == "Total Wine" and second["storeNumber"] == ""
        # The row had no address: blank, and listed.
        assert second["address"] == "" and second["streetAddress"] == ""
        assert second["couldNotRead"] == ["Street address", "City", "State", "ZIP"]

        # Parsing created and changed nothing.
        assert await sync_to_async(self._counts)() == before

    @pytest.mark.asyncio
    async def test_what_the_model_makes_up_is_left_blank(self):
        invented = _row(
            date_text="1/9", month=1, day=9, year=None, weekday_text="Monday",
            start_text="4", end_text="7pm",
            store_name="Spec's",
            # None of this is in the email: an address "from memory", a ZIP,
            # a state, a note, and a product from another brand's catalog.
            street_address="2410 Smith St", city="Houston", state="TX", zip="77006",
            notes="Bring a branded tent and two coolers",
            products=["Mango Chainsaw", "Grim Leafer", "Zombie Punch"],
        )
        with mock.patch(POST_JSON, return_value=_reply(invented)) as post, mock.patch(
            "events.live_board._ops_today", return_value=TODAY
        ):
            res = await self._parse(EMAIL)
            sent = json.dumps(post.call_args.args[1])
        # The signature, the phone number and the address never left Spark.
        for private in ("Jordan", "example.com", "555-0142", "Regional Manager"):
            assert private not in sent
        assert res.errors is None, res.errors
        draft = res.data["parseRequestDetails"]["drafts"][0]

        assert draft["date"] == "2027-01-09" and draft["yearAssumed"] is True
        # 9 January 2027 is a Saturday; the model said Monday.
        assert "Saturday" in draft["dateWarning"] and "Monday" in draft["dateWarning"]
        # "4 to 7pm": the 4 is settled by the 7pm, and flagged.
        assert (draft["startTime"], draft["endTime"], draft["timeAssumed"]) == ("16:00", "19:00", True)
        assert draft["startAsTyped"] == "4"
        assert draft["storeName"] == "Spec's" and draft["matchedStoreName"] == ""
        # In the text: kept. Not in the text: blank.
        assert draft["streetAddress"] == "2410 Smith St" and draft["city"] == "Houston"
        assert draft["state"] == "" and draft["zip"] == "" and draft["notes"] == ""
        assert draft["address"] == "2410 Smith St, Houston"
        assert draft["couldNotRead"] == ["State", "ZIP"]
        assert [p["name"] for p in draft["products"]] == ["Mango Chainsaw"]
        # Not written in the email at all: dropped, not offered.
        assert draft["unmatchedProducts"] == []

    @pytest.mark.asyncio
    async def test_a_product_the_brand_does_not_have_is_shown_not_added(self):
        text = "HEB 11/20/2026 10a-1p sample Mango Chainsaw and Zombie Punch"
        row = _row(
            date_text="11/20/2026", month=11, day=20, year=2026, start_text="10a",
            end_text="1p", store_name="HEB", products=["Mango Chainsaw", "Zombie Punch"],
        )
        before = await sync_to_async(self._counts)()
        with mock.patch(POST_JSON, return_value=_reply(row)):
            res = await self._parse(text)
        draft = res.data["parseRequestDetails"]["drafts"][0]
        assert [p["name"] for p in draft["products"]] == ["Mango Chainsaw"]
        assert draft["unmatchedProducts"] == ["Zombie Punch"]
        assert await sync_to_async(self._counts)() == before

    @pytest.mark.asyncio
    async def test_unreadable_fields_stay_blank(self):
        row = _row(store_name="HEB", start_text="morning", end_text=None, date_text="next week")
        with mock.patch(POST_JSON, return_value=_reply(row)):
            res = await self._parse("Could we do HEB some time next week, morning?")
        draft = res.data["parseRequestDetails"]["drafts"][0]
        assert draft["date"] == "" and draft["startTime"] == "" and draft["endTime"] == ""
        assert draft["dateAsTyped"] == "next week" and draft["startAsTyped"] == "morning"
        assert draft["couldNotRead"] == [
            "Date", "Start time", "End time", "Street address", "City", "State", "ZIP",
        ]

    @pytest.mark.asyncio
    async def test_no_key_a_failed_call_or_nothing_found_falls_back_to_the_form(self, settings):
        from utils.ai_text import AiUnavailable

        settings.OPENAI_API_KEY = ""
        with mock.patch(POST_JSON) as post:
            res = await self._parse(SHEET_ROWS)
            post.assert_not_called()
        assert res.errors is None, res.errors
        assert res.data["parseRequestDetails"] == {
            "ok": False, "message": ri.UNAVAILABLE_MESSAGE, "truncated": False, "drafts": [],
        }

        settings.OPENAI_API_KEY = "test-key"
        with mock.patch(POST_JSON, side_effect=AiUnavailable("AI service timed out.")):
            res = await self._parse(SHEET_ROWS)
        assert res.data["parseRequestDetails"]["ok"] is False
        assert res.data["parseRequestDetails"]["message"] == ri.UNAVAILABLE_MESSAGE

        with mock.patch(POST_JSON, return_value={"choices": [{"message": {"content": "not json"}}]}):
            res = await self._parse(SHEET_ROWS)
        assert res.data["parseRequestDetails"]["message"] == ri.UNAVAILABLE_MESSAGE

        with mock.patch(POST_JSON, return_value=_reply()):
            res = await self._parse("Hope you had a good weekend!")
        assert res.data["parseRequestDetails"] == {
            "ok": False, "message": ri.NOTHING_FOUND_MESSAGE, "truncated": False, "drafts": [],
        }

        with mock.patch(POST_JSON) as post:
            res = await self._parse("   ")
            post.assert_not_called()
        assert res.data["parseRequestDetails"]["ok"] is False

    @pytest.mark.asyncio
    async def test_at_most_ten_drafts_come_back(self):
        rows = [_row(store_name="HEB", month=11, day=20, year=2026) for _ in range(14)]
        with mock.patch(POST_JSON, return_value=_reply(*rows)):
            res = await self._parse("HEB " * 14 + "11/20/2026")
        out = res.data["parseRequestDetails"]
        assert len(out["drafts"]) == ri.MAX_DRAFTS and out["truncated"] is True

    @pytest.mark.asyncio
    async def test_a_brand_user_cannot_call_it(self):
        with mock.patch(POST_JSON) as post:
            own = await self._parse(SHEET_ROWS, user=self.brand_user)
            other = await self._parse(SHEET_ROWS, user=self.brand_user, tenant=self.other)
            post.assert_not_called()
        assert own.errors is not None and "Staff only" in str(own.errors[0])
        assert other.errors is not None and "Staff only" in str(other.errors[0])

    @pytest.mark.asyncio
    async def test_an_ambassador_cannot_call_it(self):
        ba = await sync_to_async(self.create_user)(
            username="intake-ba", email="intake-ba@test.com", role=self.roles["ambassador"]
        )
        with mock.patch(POST_JSON) as post:
            res = await self._parse(SHEET_ROWS, user=ba)
            post.assert_not_called()
        assert res.errors is not None

    @pytest.mark.asyncio
    async def test_the_pasted_text_is_not_logged(self, caplog):
        import logging

        with caplog.at_level(logging.DEBUG), mock.patch(
            POST_JSON, return_value=_reply(ROW_ONE)
        ):
            await self._parse(SHEET_ROWS)
        assert "2701 E 7th St" not in caplog.text
        assert "beverage manager" not in caplog.text
