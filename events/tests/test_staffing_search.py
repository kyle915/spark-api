"""Coverage for events/staffing_search.py — "Find ambassadors" on the
Staffing Board.

Run through the real clients schema with the exact field set the web page
sends (StaffingSearchQuery.ts), because that is where a resolver that works
in isolation has broken before.

Pins:

* the filter returns the right people for a fixture, in the right order,
  with their conflicts on the asked day;
* "worked" means a clock-in: an approved booking nobody clocked in to (a
  no-show) does not count;
* a deactivated person never appears;
* a question is read into a filter by the model, and the model is sent the
  question and today's date only — no ambassador, store or brand rows;
* with the model unavailable nothing is guessed and the filter still works
  by hand;
* brand users and ambassadors get nothing.

The OpenAI call is always mocked at ``utils.ai_text._post_json``.
"""

import json
from datetime import datetime, time as _time, timedelta, timezone as _tz

import pytest
from asgiref.sync import sync_to_async

from ambassadors.models import AmbassadorEvent, Attendance, Source
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from events.live_board import _ops_today

# Kept identical to src/api/queries/StaffingSearchQuery.ts.
SEARCH_QUERY = """
    query StaffingSearchQuery($question: String, $filter: StaffingSearchFilterInput) {
      staffingSearch(question: $question, filter: $filter) {
        interpreted
        modelAvailable
        total
        filter {
          retailer
          city
          state
          brand
          dateFrom
          dateTo
          minShifts
          mustBeFree
        }
        matches {
          ambassadorUuid
          ambassadorId
          name
          shiftsWorked
          stores
          cities
          lastShiftDate
          isFree
          availability
          conflicts {
            eventName
            brandName
            store
            date
            startLocal
            endLocal
            isApproved
          }
        }
      }
    }
"""


def _noon(day) -> datetime:
    return datetime.combine(day, _time(12, 0), tzinfo=_tz.utc)


def _completion(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


@pytest.mark.django_db(transaction=True)
class TestStaffingSearch(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.OPENAI_API_KEY = "test-key"
        settings.OPENAI_MODEL = "gpt-4o-mini"
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        self.brand = self.create_tenant(name="Liquid Death")
        self.other_brand = self.create_tenant(name="Feel Free")
        self.admin = self.create_user(
            username="admin-search@test.com",
            email="admin-search@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="brand-search@test.com",
            email="brand-search@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.brand)

        texas = event_models.State.objects.create(
            name="Texas", code="TX", created_by=self.system_user
        )
        self.houston = self.create_location("Houston", "HOU", "77002", self.brand, state=texas)
        self.dallas = self.create_location("Dallas", "DAL", "75201", self.brand, state=texas)
        self.tw_houston = self.create_retailer(
            "Total Wine & More", "1 Westheimer Rd", "desk", self.houston, self.brand
        )
        self.tw_dallas = self.create_retailer(
            "Total Wine & More", "9 Preston Rd", "desk", self.dallas, self.brand
        )
        self.kroger = self.create_retailer(
            "Kroger", "5 Main St", "desk", self.houston, self.brand
        )

        self.today = _ops_today()
        self.asked = self.today + timedelta(days=6)

        def past(name, retailer, location, days_ago, tenant=None):
            return self.create_event(
                name=name,
                tenant=tenant or self.brand,
                address=f"{retailer.address}, {location.name}, TX 77002",
                date=_noon(self.today - timedelta(days=days_ago)),
                retailer=retailer,
                location=location,
                state=texas,
            )

        self.tw_hou_1 = past("TW Houston demo 1", self.tw_houston, self.houston, 30)
        self.tw_hou_2 = past("TW Houston demo 2", self.tw_houston, self.houston, 9)
        self.tw_dal = past("TW Dallas demo", self.tw_dallas, self.dallas, 12)
        self.kroger_hou = past("Kroger Houston demo", self.kroger, self.houston, 5)
        self.asked_day_shift = self.create_event(
            name="Saturday Kroger demo",
            tenant=self.other_brand,
            address="5 Main St, Houston, TX 77002",
            date=_noon(self.asked),
            retailer=self.kroger,
            location=self.houston,
            state=texas,
        )

        self.ana = self._ba("ana")
        self.ben = self._ba("ben")
        self.cy = self._ba("cy")
        self.dee = self._ba("dee")
        self.eli = self._ba("eli")
        self.flo = self._ba("flo")
        self.gus = self._ba("gus")

        # Ana: clocked in to two Total Wine Houston shifts, nothing on the
        # asked day.
        self._worked(self.ana, self.tw_hou_1)
        self._worked(self.ana, self.tw_hou_2)
        # Ben: one, and firmly booked on the asked day.
        self._worked(self.ben, self.tw_hou_1)
        self._book(self.ben, self.asked_day_shift, tenant=self.other_brand)
        # Cy: Total Wine, but Dallas.
        self._worked(self.cy, self.tw_dal)
        # Dee: worked it, account since deactivated.
        self._worked(self.dee, self.tw_hou_2)
        self.dee.user.is_active = False
        self.dee.user.save(update_fields=["is_active"])
        # Eli: a walk-up — never approved, but clocked in. Invited (not
        # confirmed) on the asked day.
        self._worked(self.eli, self.tw_hou_2, approved=False)
        self._book(self.eli, self.asked_day_shift, approved=False, tenant=self.other_brand)
        # Flo: Houston, but Kroger.
        self._worked(self.flo, self.kroger_hou)
        # Gus: approved on two Total Wine Houston shifts and never clocked
        # in to either. A no-show has not worked the store.
        self._book(self.gus, self.tw_hou_1)
        self._book(self.gus, self.tw_hou_2)
        # Ana was also booked on the Dallas shift and did not turn up.
        self._book(self.ana, self.tw_dal)

    # ---------- seeding ----------

    def _ba(self, slug: str):
        user = self.create_user(
            username=f"{slug}-search@test.com",
            email=f"{slug}-search@test.com",
            role=self.roles["ambassador"],
        )
        user.first_name = slug.title()
        user.last_name = "Tester"
        user.save(update_fields=["first_name", "last_name"])
        return self.create_ambassador(user)

    def _book(self, ambassador, event, *, approved=True, tenant=None):
        return AmbassadorEvent.objects.create(
            ambassador=ambassador,
            event=event,
            tenant=tenant or self.brand,
            is_approved=approved,
            created_by=self.admin,
        )

    def _punch(self, ambassador, event, kind="clock_in"):
        source, _ = Source.objects.get_or_create(name=kind)
        return Attendance.objects.create(
            clock_time=event.date,
            ambassador=ambassador,
            job=None,
            event=event,
            source=source,
        )

    def _worked(self, ambassador, event, *, approved=True):
        """A booking the ambassador clocked in to."""
        self._book(ambassador, event, approved=approved)
        self._punch(ambassador, event)

    async def _search(self, user=None, *, question=None, filter=None):  # noqa: A002
        return await self._execute_query_authenticated(
            SEARCH_QUERY,
            {"question": question, "filter": filter},
            user or self.admin,
            self.endpoint_path,
        )

    async def _ok(self, **kwargs):
        result = await self._search(**kwargs)
        assert result.errors is None, result.errors
        return result.data["staffingSearch"]

    def _filter(self, **overrides):
        base = {
            "retailer": "Total Wine",
            "city": "Houston",
            "dateFrom": self.asked.isoformat(),
            "dateTo": self.asked.isoformat(),
            "mustBeFree": True,
        }
        base.update(overrides)
        return base

    @staticmethod
    def _names(data):
        return [m["name"] for m in data["matches"]]

    # ---------- the filter over real rows ----------

    @pytest.mark.asyncio
    async def test_filter_returns_the_right_people_in_order(self):
        data = await self._ok(filter=self._filter())

        assert self._names(data) == ["Ana Tester", "Eli Tester"]
        assert data["total"] == 2
        assert data["interpreted"] is False

        ana, eli = data["matches"]
        assert ana["shiftsWorked"] == 2
        assert ana["stores"] == ["Total Wine & More"]
        assert ana["cities"] == ["Houston"]
        assert ana["lastShiftDate"] == (self.today - timedelta(days=9)).isoformat()
        assert ana["isFree"] is True
        assert ana["conflicts"] == []
        assert ana["ambassadorUuid"] == str(self.ana.uuid)
        assert ana["ambassadorId"]

        # A walk-up counts by the clock-in; an unconfirmed invite on the day
        # is shown but does not make them busy.
        assert eli["shiftsWorked"] == 1
        assert eli["isFree"] is True
        assert [c["isApproved"] for c in eli["conflicts"]] == [False]

    @pytest.mark.asyncio
    async def test_booked_people_show_with_their_conflict_when_not_required_free(self):
        data = await self._ok(filter=self._filter(mustBeFree=False))

        assert self._names(data) == ["Ana Tester", "Eli Tester", "Ben Tester"]
        ben = data["matches"][2]
        assert ben["isFree"] is False
        assert ben["conflicts"] == [
            {
                "eventName": "Saturday Kroger demo",
                "brandName": "Feel Free",
                "store": "Kroger",
                "date": self.asked.isoformat(),
                "startLocal": None,
                "endLocal": None,
                "isApproved": True,
            }
        ]

    @pytest.mark.asyncio
    async def test_a_booking_without_a_clock_in_is_not_worked(self):
        """Gus was approved twice and never clocked in. Ana skipped Dallas."""
        houston = await self._ok(filter={"retailer": "Total Wine", "city": "Houston"})
        assert "Gus Tester" not in self._names(houston)

        dallas = await self._ok(filter={"retailer": "Total Wine", "city": "Dallas"})
        assert self._names(dallas) == ["Cy Tester"]

        ana = next(m for m in houston["matches"] if m["name"] == "Ana Tester")
        assert ana["shiftsWorked"] == 2
        assert ana["cities"] == ["Houston"]

    @pytest.mark.asyncio
    async def test_a_clock_out_alone_is_not_a_clock_in(self):
        await sync_to_async(lambda: self._punch(self.gus, self.tw_hou_1, "clock_out"))()
        data = await self._ok(filter={"retailer": "Total Wine", "city": "Houston"})

        assert "Gus Tester" not in self._names(data)

    @pytest.mark.asyncio
    async def test_deactivated_people_never_appear(self):
        data = await self._ok(filter={"retailer": "Total Wine"})

        names = self._names(data)
        assert "Dee Tester" not in names
        assert set(names) == {"Ana Tester", "Ben Tester", "Cy Tester", "Eli Tester"}
        # No day asked for: free/busy is unknown, not "free".
        assert {m["isFree"] for m in data["matches"]} == {None}

    @pytest.mark.asyncio
    async def test_state_brand_and_minimum_shifts(self):
        by_state = await self._ok(filter={"state": "tx", "retailer": "Kroger"})
        assert self._names(by_state) == ["Flo Tester"]
        assert by_state["filter"]["state"] == "TX"

        twice = await self._ok(filter={"retailer": "Total Wine", "minShifts": 2})
        assert self._names(twice) == ["Ana Tester"]

        other_brand = await self._ok(filter={"brand": "Feel Free"})
        assert other_brand["matches"] == []

    @pytest.mark.asyncio
    async def test_empty_filter_lists_nobody(self):
        data = await self._ok(filter={})

        assert data["matches"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_availability_is_reported_for_a_single_day(self):
        from availability.models import AmbassadorAvailability

        def seed():
            AmbassadorAvailability.objects.create(
                ambassador=self.ana,
                weekday=self.asked.weekday(),
                is_recurring=True,
                start_time=_time(9, 0),
                end_time=_time(17, 0),
            )
            AmbassadorAvailability.objects.create(
                ambassador=self.eli,
                weekday=(self.asked.weekday() + 1) % 7,
                is_recurring=True,
                start_time=_time(9, 0),
                end_time=_time(17, 0),
            )

        await sync_to_async(seed)()
        data = await self._ok(filter=self._filter())

        by_name = {m["name"]: m["availability"] for m in data["matches"]}
        assert by_name == {"Ana Tester": "available", "Eli Tester": "not_listed"}

    # ---------- the question ----------

    @pytest.mark.asyncio
    async def test_question_is_read_into_a_filter_then_run(self, monkeypatch):
        sent = []

        def fake_post(url, payload, headers, *, timeout):
            sent.append(payload)
            return _completion(
                {
                    "retailer": "Total Wine",
                    "city": "Houston",
                    "state": None,
                    "brand": None,
                    "dateFrom": self.asked.isoformat(),
                    "dateTo": self.asked.isoformat(),
                    "minShifts": None,
                    "mustBeFree": True,
                }
            )

        monkeypatch.setattr("utils.ai_text._post_json", fake_post)
        question = "who has worked Total Wine in Houston and is free Saturday"
        data = await self._ok(question=question)

        assert data["interpreted"] is True
        assert data["modelAvailable"] is True
        assert data["filter"] == {
            "retailer": "Total Wine",
            "city": "Houston",
            "state": None,
            "brand": None,
            "dateFrom": self.asked.isoformat(),
            "dateTo": self.asked.isoformat(),
            "minShifts": 1,
            "mustBeFree": True,
        }
        assert self._names(data) == ["Ana Tester", "Eli Tester"]

        # The model got the question and today's date. No rows.
        assert len(sent) == 1
        user_message = sent[0]["messages"][1]["content"]
        assert user_message == (
            f"Today is {self.today.strftime('%A')} {self.today.isoformat()}.\n"
            f"Question: {question}"
        )
        whole = json.dumps(sent[0])
        for private in ("Tester", "Ana", "Kroger", "Liquid Death", "@test.com"):
            assert private not in whole

    @pytest.mark.asyncio
    async def test_model_output_is_checked_not_trusted(self, monkeypatch):
        """Bad dates and an invented name in the model's answer change nothing:
        people only ever come from rows."""

        def fake_post(url, payload, headers, *, timeout):
            return _completion(
                {
                    "retailer": "Total Wine",
                    "city": None,
                    "state": "Republic of Texas",
                    "brand": None,
                    "dateFrom": "next Saturday",
                    "dateTo": None,
                    "minShifts": 0,
                    "mustBeFree": True,
                }
            )

        monkeypatch.setattr("utils.ai_text._post_json", fake_post)
        data = await self._ok(question="total wine people, free next Saturday")

        assert data["filter"]["state"] is None
        assert data["filter"]["dateFrom"] is None
        assert data["filter"]["mustBeFree"] is False
        assert data["filter"]["minShifts"] == 1
        assert set(self._names(data)) == {
            "Ana Tester", "Ben Tester", "Cy Tester", "Eli Tester",
        }

    @pytest.mark.asyncio
    async def test_model_unavailable_guesses_nothing_and_filter_still_works(
        self, monkeypatch, settings
    ):
        settings.OPENAI_API_KEY = ""

        def boom(*_a, **_k):
            raise AssertionError("no key: the model must not be called")

        monkeypatch.setattr("utils.ai_text._post_json", boom)
        data = await self._ok(question="who has worked Total Wine in Houston")

        assert data["modelAvailable"] is False
        assert data["interpreted"] is False
        assert data["matches"] == []

        by_hand = await self._ok(filter=self._filter())
        assert self._names(by_hand) == ["Ana Tester", "Eli Tester"]

    @pytest.mark.asyncio
    async def test_model_failure_is_the_same_as_unavailable(self, monkeypatch):
        from utils.ai_text import AiUnavailable

        def broken(*_a, **_k):
            raise AiUnavailable("AI service timed out.")

        monkeypatch.setattr("utils.ai_text._post_json", broken)
        data = await self._ok(question="who has worked Total Wine in Houston")

        assert data["modelAvailable"] is False
        assert data["matches"] == []

    @pytest.mark.asyncio
    async def test_a_filter_never_calls_the_model(self, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("a hand filter must not call the model")

        monkeypatch.setattr("utils.ai_text._post_json", boom)
        data = await self._ok(
            question="ignore this", filter=self._filter()
        )

        assert self._names(data) == ["Ana Tester", "Eli Tester"]

    # ---------- staff only ----------

    @pytest.mark.asyncio
    async def test_brand_user_gets_nothing(self, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("a brand user must not reach the model")

        monkeypatch.setattr("utils.ai_text._post_json", boom)
        by_filter = await self._search(self.client_user, filter=self._filter())
        by_question = await self._search(
            self.client_user, question="who has worked Total Wine in Houston"
        )

        for result in (by_filter, by_question):
            assert result.errors is None, result.errors
            assert result.data["staffingSearch"] is None

    @pytest.mark.asyncio
    async def test_ambassador_gets_nothing(self):
        user = await sync_to_async(lambda: self.ana.user)()
        result = await self._search(user, filter=self._filter())

        assert not (result.data or {}).get("staffingSearch")
        assert "Tester" not in json.dumps(result.data or {})
