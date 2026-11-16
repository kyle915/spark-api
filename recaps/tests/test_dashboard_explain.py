"""The dashboard sentences: what changed, callouts, a question about one
chart, and where to sample next.

Runs through the real ``schema_clients`` with the field sets the web client
asks for and the model helper mocked. The live API is never called.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from events import models as event_models
from recaps import models as recap_models
from recaps.explain_text import (
    Draft,
    accept,
    numbers_in,
    unsupported_numbers,
    window_key,
    wrong_direction,
)
from utils.ai_text import AiUnavailable


# ─── The number check ───────────────────────────────────────────────

FACTS = (
    "Consumers sampled: 1,284 this period, 1,088 before.\n"
    "Consumers sampled is up 18%.\n"
    "Main reason: 12 new stores in Texas added 196.\n"
    "Conversion rate: 14.2% before, 11% this period."
)


def test_numbers_are_read_whatever_their_formatting():
    assert numbers_in("1,284 against 1088, up 18% (14.2%)") == [1284, 1088, 18, 14.2]


@pytest.mark.parametrize(
    "text",
    [
        "Consumers sampled rose 18%, to 1,284 from 1,088.",
        "Up 18% (1284 vs 1088), helped by 12 new stores in Texas.",
        "Conversion slipped to 11% from 14.2%.",
    ],
)
def test_supplied_numbers_pass(text):
    assert unsupported_numbers(text, FACTS) == []


@pytest.mark.parametrize(
    "text, bad",
    [
        ("Consumers sampled rose 19%.", "19"),
        ("Up 18%, a gain of 200 people.", "200"),
        ("Twelve new stores opened and five closed.", "five"),
        ("Consumers sampled nearly doubled.", "doubled"),
        ("About half came from Texas.", "half"),
        ("Conversion fell by 3.2 points.", "3.2"),
    ],
)
def test_a_number_nobody_supplied_is_caught(text, bad):
    assert bad in unsupported_numbers(text, FACTS)


def test_a_number_word_that_is_a_supplied_number_passes():
    assert unsupported_numbers("Twelve new stores in Texas.", FACTS) == []


def test_a_store_name_with_a_number_in_it_passes():
    facts = "Store: Five Below #42.\nIts conversion rate: 31%."
    assert unsupported_numbers("Five Below #42 converted at 31%.", facts) == []


def test_saying_a_measure_went_the_other_way_is_caught():
    directions = {"consumers": "up", "conversion": "down"}
    assert wrong_direction("Consumers sampled fell 18%.", directions)
    assert wrong_direction("Consumers sampled is up. Conversion rate rose.", directions)
    assert not wrong_direction("Consumers sampled rose 18%. Conversion fell.", directions)
    # One sentence naming both measures may hold both words.
    assert not wrong_direction("Consumers rose while conversion fell.", directions)


def test_accept_keeps_only_text_that_checks_out():
    draft = Draft("k", "Consumers sampled is up 18%.", FACTS, {"consumers": "up"})
    assert accept("Consumers sampled rose 18%, to 1,284.", draft)
    assert not accept("Consumers sampled rose 81%.", draft)
    assert not accept("Consumers sampled fell 18%.", draft)
    assert not accept("", draft)
    assert not accept("x" * 500, draft)


def test_window_key_does_not_change_daily_for_rolling_ranges():
    today = date(2026, 11, 15)
    assert window_key(date(2026, 10, 16), today, today) == "31d"
    assert window_key(date(2026, 1, 1), today, today) == "ytd"
    assert window_key(date(2026, 10, 1), date(2026, 10, 31), today) == "2026-10-01:2026-10-31"
    assert window_key(None, None, today) == "all"


# ─── Through the database and the real schema ───────────────────────

# The exact field sets the web client asks for.
WHAT_CHANGED_Q = """
query W($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantWhatChanged(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    text
    source
    previousStartDate
    previousEndDate
  }
}
"""
CALLOUTS_Q = """
query C($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantCallouts(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    key
    kind
    text
    numbers { label value }
    link { store state startDate endDate }
  }
}
"""
CHART_Q = """
query A($tenantId: ID!, $chart: String!, $question: String!, $startDate: String, $endDate: String) {
  chartAnswer(
    tenantId: $tenantId
    chart: $chart
    question: $question
    startDate: $startDate
    endDate: $endDate
  ) { ok answered answer reason }
}
"""
SAMPLE_NEXT_Q = """
query S($tenantId: ID!) {
  tenantSampleNext(tenantId: $tenantId) {
    items { key kind name address market reason lat lng }
  }
}
"""

GENERATE = "utils.ai_text.generate_json"


def _reworded(suffix=""):
    """A model that returns each draft unchanged (plus ``suffix``)."""

    def _fake(system, user, **kwargs):
        items = []
        for block in user.split("\n\nITEM ")[0:]:
            block = block.removeprefix("ITEM ")
            key = block.split("\n", 1)[0].strip()
            draft = block.rsplit("DRAFT: ", 1)[-1].strip()
            items.append({"key": key, "text": draft + suffix})
        return {"items": items}

    return _fake


@pytest.mark.django_db(transaction=True)
class TestDashboardExplain(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.live_board import _ops_today

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.today = _ops_today()
        self.start = self.today - timedelta(days=29)
        self.tenant = self.create_tenant(name="Explain Brand")
        self.other = self.create_tenant(name="Explain Other")
        self.kit = {t.id: self._kit(t) for t in (self.tenant, self.other)}
        self.ca = event_models.State.objects.create(
            name="California", code="CA", created_by=self.sys
        )
        self.tx = event_models.State.objects.create(
            name="Texas", code="TX", created_by=self.sys
        )
        self.costco = self._retailer("Costco")
        self.kroger = self._retailer("Kroger")
        self.admin = self.create_user(
            username="explain-admin", email="explain-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="explain-client", email="explain-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        credited = self.create_user(
            username="jordan-rivera-explain", email="jordan.rivera.explain@ba.test",
            role=self.roles["ambassador"],
        )
        credited.first_name, credited.last_name = "Jordan", "Rivera"
        credited.save()
        self.ba = self.create_ambassador(credited)

    # ── fixtures ──────────────────────────────────────────────────────

    def _kit(self, tenant):
        et = event_models.EventType.objects.create(
            name="Retail Sampling", slug=f"explain-retail-{tenant.id}", tenant=tenant,
            created_by=self.sys,
        )
        template = recap_models.CustomRecapTemplate.objects.create(
            name="Explain Retail", event_type=et, tenant=tenant, created_by=self.sys,
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
            "sampled": field("Total number of consumers sampled"),
            "sold": field("Cans sold"),
        }

    def _retailer(self, name, tenant=None):
        return event_models.Retailer.objects.create(
            name=name, tenant=tenant or self.tenant, created_by=self.sys
        )

    def _recap(
        self,
        days_ago,
        sampled,
        sold=0,
        *,
        retailer=None,
        state=None,
        address="1 Main St",
        tenant=None,
        approved=True,
        archived=False,
        request=None,
    ):
        tenant = tenant or self.tenant
        kit = self.kit[tenant.id]
        when = timezone.make_aware(
            datetime.combine(self.today - timedelta(days=days_ago), time(18))
        )
        event = event_models.Event.objects.create(
            name=f"Event {days_ago}", tenant=tenant, address=address, date=when,
            event_type=kit["event_type"], retailer=retailer, state=state,
            request=request, created_by=self.sys, updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=approved, submitted_at=when, event=event,
            tenant=tenant, custom_recap_template=kit["template"],
            ambassador=self.ba, created_by=self.sys,
            archived_at=timezone.now() if archived else None,
        )
        for field, value in ((kit["sampled"], sampled), (kit["sold"], sold)):
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=field, value=str(value),
                created_by=self.sys,
            )
        return recap

    def _deleted_request(self):
        return event_models.Request.objects.create(
            name="Deleted gig", address="1 Main St", tenant=self.tenant,
            request_type=event_models.RequestType.objects.create(
                name="Retail", tenant=self.tenant, created_by=self.sys
            ),
            created_by=self.sys, deleted_at=timezone.now(),
        )

    def _seed_growth(self, archived=True):
        """Before: 10 recaps at Costco, CA, 100 each. Now: the same, plus 4
        new stores in Texas with 100 each. 1,000 → 1,400, up 40%."""
        for i in range(10):
            self._recap(31 + i, 100, 10, retailer=self.costco, state=self.ca)
            self._recap(1 + i, 100, 10, retailer=self.costco, state=self.ca)
        for i in range(4):
            self._recap(
                2 + i, 100, 10, retailer=self.kroger, state=self.tx,
                address=f"{100 + i} Texas Ave, Houston, TX 77001",
            )
        # None of these may count, or reach the model.
        self._recap(3, 7777, retailer=self.costco, state=self.ca, approved=False)
        if archived:
            self._recap(3, 8888, retailer=self.costco, state=self.ca, archived=True)
        self._recap(
            3, 9999, retailer=self.costco, state=self.ca, request=self._deleted_request()
        )
        self._recap(3, 6666, tenant=self.other)

    def _window(self, tenant=None):
        return {
            "tenantId": str((tenant or self.tenant).id),
            "startDate": self.start.isoformat(),
            "endDate": self.today.isoformat(),
        }

    async def _run(self, query, variables, user):
        res = await self._execute_mutation(
            query, variables, self.endpoint_path, user=user
        )
        assert res.errors is None, res.errors
        return res.data

    # ── what changed ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_what_changed_names_the_move_and_what_accounts_for_it(self):
        await sync_to_async(self._seed_growth)()
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            data = await self._run(WHAT_CHANGED_Q, self._window(), self.client_user)
        out = data["tenantWhatChanged"]
        assert out["source"] == "template"
        assert out["text"] == (
            "Consumers sampled is up 40% on the period before "
            "(1,400 against 1,000), mostly from 4 new stores in Texas."
        )
        assert out["previousEndDate"] == (self.start - timedelta(days=1)).isoformat()
        assert out["previousStartDate"] == (self.start - timedelta(days=30)).isoformat()

    @pytest.mark.asyncio
    async def test_the_prompt_holds_approved_recaps_only_and_no_ambassador(self):
        await sync_to_async(self._seed_growth)()
        with patch(GENERATE, side_effect=_reworded()) as generate:
            data = await self._run(WHAT_CHANGED_Q, self._window(), self.client_user)
        assert data["tenantWhatChanged"]["source"] == "model"
        prompt = generate.call_args[0][1]
        assert "1,400" in prompt and "1,000" in prompt
        for hidden in ("7,777", "8,888", "9,999", "6,666", "9,177", "Rivera", "Jordan"):
            assert hidden not in prompt, hidden

    @pytest.mark.asyncio
    async def test_a_number_the_model_made_up_falls_back_to_the_template(self):
        await sync_to_async(self._seed_growth)()

        def _wrong(system, user, **kwargs):
            return {
                "items": [
                    {
                        "key": "what_changed",
                        "text": "Consumers sampled jumped 55% thanks to Texas.",
                    }
                ]
            }

        with patch(GENERATE, side_effect=_wrong):
            data = await self._run(WHAT_CHANGED_Q, self._window(), self.client_user)
        out = data["tenantWhatChanged"]
        assert out["source"] == "template"
        assert "55" not in out["text"]
        assert out["text"].startswith("Consumers sampled is up 40%")

    @pytest.mark.asyncio
    async def test_a_model_that_returns_nothing_usable_falls_back(self):
        await sync_to_async(self._seed_growth)()
        with patch(GENERATE, return_value=None):
            data = await self._run(WHAT_CHANGED_Q, self._window(), self.client_user)
        out = data["tenantWhatChanged"]
        assert out["source"] == "template"
        assert out["text"].startswith("Consumers sampled is up 40%")

    @pytest.mark.asyncio
    async def test_too_few_recaps_says_nothing_and_does_not_ask_the_model(self):
        def _thin():
            for i in range(9):  # one short of the floor, before
                self._recap(31 + i, 100, retailer=self.costco, state=self.ca)
            for i in range(12):
                self._recap(1 + i, 100, retailer=self.costco, state=self.ca)

        await sync_to_async(_thin)()
        with patch(GENERATE) as generate:
            data = await self._run(WHAT_CHANGED_Q, self._window(), self.client_user)
        assert data["tenantWhatChanged"] == {
            "text": "",
            "source": "none",
            "previousStartDate": None,
            "previousEndDate": None,
        }
        generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_second_load_does_not_ask_the_model_until_numbers_change(self):
        await sync_to_async(self._seed_growth)()
        with patch(GENERATE, side_effect=_reworded()) as generate:
            first = await self._run(WHAT_CHANGED_Q, self._window(), self.client_user)
            again = await self._run(WHAT_CHANGED_Q, self._window(), self.admin)
            assert generate.call_count == 1
            assert first == again
            await sync_to_async(self._recap)(
                1, 200, retailer=self.costco, state=self.ca
            )
            changed = await self._run(WHAT_CHANGED_Q, self._window(), self.client_user)
            assert generate.call_count == 2
        assert "1,600" in changed["tenantWhatChanged"]["text"]
        assert await sync_to_async(
            recap_models.DashboardText.objects.filter(kind="what_changed").count
        )() == 1

    @pytest.mark.asyncio
    async def test_a_brand_user_is_held_to_their_own_brand_and_staff_may_choose(self):
        await sync_to_async(self._seed_growth)()
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            mine = await self._run(
                WHAT_CHANGED_Q, self._window(self.other), self.client_user
            )
            theirs = await self._run(WHAT_CHANGED_Q, self._window(self.other), self.admin)
        # The client asked about the other brand and got their own.
        assert mine["tenantWhatChanged"]["text"].startswith("Consumers sampled is up 40%")
        # Staff really did read the other brand: one recap, so nothing to say.
        assert theirs["tenantWhatChanged"]["text"] == ""

    # ── callouts ──────────────────────────────────────────────────────

    def _seed_stores(self):
        """Brand average near 10%. Kroger converts at 40% over 3 recaps. One
        huge single recap at Target must not raise anything."""
        target = self._retailer("Target")
        for i in range(10):
            self._recap(1 + i, 100, 10, retailer=self.costco, state=self.ca)
        for i in range(3):
            self._recap(2 + i, 100, 40, retailer=self.kroger, state=self.tx)
        self._recap(4, 60, 55, retailer=target, state=self.ca)
        self._recap(3, 100, 99, retailer=self.kroger, state=self.tx, approved=False)

    @pytest.mark.asyncio
    async def test_a_store_far_above_average_is_called_out_with_its_numbers(self):
        await sync_to_async(self._seed_stores)()
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            data = await self._run(CALLOUTS_Q, self._window(), self.client_user)
        callouts = data["tenantCallouts"]
        assert [c["kind"] for c in callouts] == ["store_above_average"]
        kroger = callouts[0]
        # 120 of 300 at Kroger; brand: 100 + 120 + 55 sold of 1,360 sampled.
        assert kroger["text"] == (
            "Kroger converted at 40% across 3 approved recaps, "
            "well above the 20.2% average."
        )
        assert {"label": "Units sold", "value": "120"} in kroger["numbers"]
        assert {"label": "People sampled", "value": "300"} in kroger["numbers"]
        assert kroger["link"] == {
            "store": "Kroger",
            "state": None,
            "startDate": self.start.isoformat(),
            "endDate": self.today.isoformat(),
        }
        assert all("Target" not in c["text"] for c in callouts)

    @pytest.mark.asyncio
    async def test_each_callout_is_checked_on_its_own(self):
        await sync_to_async(self._seed_stores)()
        with patch(GENERATE, side_effect=_reworded(" That is 3 times the rest.")):
            data = await self._run(CALLOUTS_Q, self._window(), self.admin)
        # "3" is a supplied number (3 approved recaps), so this one passes…
        assert data["tenantCallouts"][0]["text"].endswith("That is 3 times the rest.")
        await sync_to_async(recap_models.DashboardText.objects.all().delete)()
        with patch(GENERATE, side_effect=_reworded(" That is 9 times the rest.")):
            data = await self._run(CALLOUTS_Q, self._window(), self.admin)
        # …and a number nobody supplied does not.
        assert data["tenantCallouts"][0]["text"].endswith("the 20.2% average.")

    @pytest.mark.asyncio
    async def test_a_brand_with_too_little_data_gets_no_callouts(self):
        def _thin():
            for i in range(4):
                self._recap(1 + i, 100, 10, retailer=self.costco, state=self.ca)
            for i in range(3):
                self._recap(2 + i, 100, 60, retailer=self.kroger, state=self.tx)

        await sync_to_async(_thin)()
        with patch(GENERATE) as generate:
            data = await self._run(CALLOUTS_Q, self._window(), self.client_user)
        assert data["tenantCallouts"] == []
        generate.assert_not_called()

    def test_a_state_gone_quiet_and_a_weekly_drop(self):
        from recaps.callouts import (
            _quiet_state_callouts,
            _week_drop_callouts,
            week_start,
        )

        # Texas: 3 recaps five to seven weeks ago, none since. California
        # keeps filing, so the program itself is not what went quiet.
        for days in (36, 43, 50):
            self._recap(days, 100, retailer=self.kroger, state=self.tx)
        this_monday = week_start(self.today)
        last = (self.today - this_monday).days + 7  # days ago of last Monday
        for offset in range(3):
            self._recap(last - offset, 20, retailer=self.costco, state=self.ca)
            self._recap(last + 7 - offset, 100, retailer=self.costco, state=self.ca)

        quiet = _quiet_state_callouts(self.tenant.id, self.today)
        assert [c["key"] for c in quiet] == ["state:TX"]
        assert quiet[0]["template"].startswith(
            "Texas has had no approved recap for 5 weeks. It had 3 approved recaps"
        )
        assert quiet[0]["link"]["state"] == "TX"

        drop = _week_drop_callouts(self.tenant.id, self.today)
        assert len(drop) == 1
        assert "fell 80%" in drop[0]["template"]
        assert "to 60 from 300" in drop[0]["template"]
        assert drop[0]["link"]["start"] == this_monday - timedelta(days=7)

    def test_one_small_week_does_not_read_as_a_drop(self):
        from recaps.callouts import _week_drop_callouts, week_start

        last = (self.today - week_start(self.today)).days + 7
        for offset in range(3):
            self._recap(last + 7 - offset, 100, retailer=self.costco, state=self.ca)
        # Only one approved recap last week: approvals may simply be behind.
        self._recap(last, 10, retailer=self.costco, state=self.ca)
        assert _week_drop_callouts(self.tenant.id, self.today) == []

    def test_a_week_still_in_review_is_not_called_a_drop(self):
        from recaps.callouts import _week_drop_callouts, week_start

        last = (self.today - week_start(self.today)).days + 7
        for offset in range(3):
            self._recap(last - offset, 20, retailer=self.costco, state=self.ca)
            self._recap(last + 7 - offset, 100, retailer=self.costco, state=self.ca)
        assert len(_week_drop_callouts(self.tenant.id, self.today)) == 1
        # One recap from the latest week is filed and waiting for review.
        self._recap(last, 90, retailer=self.costco, state=self.ca, approved=False)
        assert _week_drop_callouts(self.tenant.id, self.today) == []

    def _seed_texas_then_nothing_approved(self):
        """Texas: 3 approved recaps five to seven weeks ago, none since.
        California files every day, so the program is not what went quiet
        and no week reads as a drop."""
        for days in (36, 43, 50):
            self._recap(days, 100, retailer=self.kroger, state=self.tx)
        for days in range(1, 22):
            self._recap(days, 100, retailer=self.costco, state=self.ca)

    def _seed_texas_in_review(self):
        self._seed_texas_then_nothing_approved()
        # Filed in the last 4 weeks, not yet approved: these two count.
        self._recap(5, 80, retailer=self.kroger, state=self.tx, approved=False)
        self._recap(
            12, 80, retailer=self.kroger, approved=False,
            address="9 Texas Ave, Houston, TX 77001",
        )
        # These do not: archived, on a deleted request, older than the
        # quiet window, another brand's.
        self._recap(
            6, 80, retailer=self.kroger, state=self.tx, approved=False, archived=True
        )
        self._recap(
            6, 80, retailer=self.kroger, state=self.tx, approved=False,
            request=self._deleted_request(),
        )
        self._recap(30, 80, retailer=self.kroger, state=self.tx, approved=False)
        self._recap(6, 80, state=self.tx, approved=False, tenant=self.other)

    @pytest.mark.asyncio
    async def test_staff_see_a_state_waiting_for_review_not_a_quiet_one(self):
        await sync_to_async(self._seed_texas_in_review)()
        with patch(GENERATE, side_effect=_reworded(" Really.")) as generate:
            data = await self._run(CALLOUTS_Q, self._window(), self.admin)
        callouts = data["tenantCallouts"]
        assert [c["kind"] for c in callouts] == ["state_waiting_review"]
        texas = callouts[0]
        # Worded in code, word for word: never sent to the model.
        assert texas["text"] == (
            "Texas has 2 recaps waiting for review from the last 4 weeks."
        )
        generate.assert_not_called()
        assert {"label": "Waiting for review", "value": "2"} in texas["numbers"]
        assert texas["link"] == {
            "store": None,
            "state": "TX",
            "startDate": (self.today - timedelta(days=27)).isoformat(),
            "endDate": self.today.isoformat(),
        }

    @pytest.mark.asyncio
    async def test_a_brand_user_sees_nothing_for_a_state_waiting_for_review(self):
        await sync_to_async(self._seed_texas_in_review)()
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            data = await self._run(CALLOUTS_Q, self._window(), self.client_user)
        # Not quiet, and no count of unapproved recaps either.
        assert data["tenantCallouts"] == []

    @pytest.mark.asyncio
    async def test_a_truly_quiet_state_is_still_called_out_for_everyone(self):
        await sync_to_async(self._seed_texas_then_nothing_approved)()
        for user in (self.client_user, self.admin):
            with patch(GENERATE, side_effect=AiUnavailable("off")):
                data = await self._run(CALLOUTS_Q, self._window(), user)
            callouts = data["tenantCallouts"]
            assert [c["kind"] for c in callouts] == ["state_quiet"]
            assert callouts[0]["text"] == (
                "Texas has had no approved recap for 5 weeks. "
                "It had 3 approved recaps in the 8 weeks before that."
            )
            assert callouts[0]["link"]["state"] == "TX"

    def test_a_product_with_samples_and_almost_no_sales(self):
        from recaps.callouts import _product_callouts

        kit = self.kit[self.tenant.id]
        ptype = event_models.ProductType.objects.create(
            name="Oils", tenant=self.tenant, created_by=self.sys
        )
        almond = event_models.Product.objects.create(
            name="Almond Oil", product_type=ptype, tenant=self.tenant,
            created_by=self.sys, updated_by=self.sys,
        )
        # No per-product sold line anywhere: sales are never guessed.
        plain = event_models.Product.objects.create(
            name="Plain Oil", product_type=ptype, tenant=self.tenant,
            created_by=self.sys, updated_by=self.sys,
        )
        sold_line = recap_models.CustomField.objects.create(
            name="Units sold: Almond Oil", custom_recap_template=kit["template"],
            custom_field_type=kit["sampled"].custom_field_type,
            recap_section=kit["sampled"].recap_section, created_by=self.sys,
        )
        for i in range(3):
            recap = self._recap(2 + i, 100, retailer=self.costco, state=self.ca)
            for product in (almond, plain):
                recap_models.CustomRecapProductSample.objects.create(
                    custom_recap=recap, product=product, quantity=60,
                    created_by=self.sys, updated_by=self.sys,
                )
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=sold_line, value="1" if i == 0 else "0",
                created_by=self.sys,
            )
        found = _product_callouts(self.tenant.id, self.start, self.today)
        assert [c["kind"] for c in found] == ["product_no_sales"]
        assert found[0]["template"] == (
            "Oils — Almond Oil: 180 samples handed out and 1 sold "
            "across 3 approved recaps."
        )

    def test_what_changed_mentions_conversion_only_when_it_really_moved(self):
        from recaps.what_changed import compute_what_changed, draft_what_changed

        # Costco: 20% before, 8% now. Kroger steady at 10% both times.
        for i in range(6):
            self._recap(31 + i, 100, 20, retailer=self.costco, state=self.ca)
            self._recap(1 + i, 100, 8, retailer=self.costco, state=self.ca)
            self._recap(37 + i, 100, 10, retailer=self.kroger, state=self.ca)
            self._recap(7 + i, 100, 10, retailer=self.kroger, state=self.ca)
        facts = compute_what_changed(self.tenant.id, self.start, self.today)
        draft = draft_what_changed(facts)
        assert draft.template == (
            "Consumers sampled is about level with the period before "
            "(1,200 against 1,200). Conversion rate fell from 15% to 9%, "
            "most at Costco (20% to 8%)."
        )
        assert draft.directions == {"conversion": "down"}

    # ── a question about one chart ────────────────────────────────────

    def _chart_vars(self, question, tenant=None, chart="consumers_by_store"):
        return {**self._window(tenant), "chart": chart, "question": question}

    @pytest.mark.asyncio
    async def test_a_chart_question_is_answered_from_that_charts_figures(self):
        await sync_to_async(self._seed_growth)(archived=False)
        reply = {"answerable": True, "answer": "Costco leads with 1,000 consumers sampled."}
        with patch(GENERATE, return_value=reply) as generate:
            data = await self._run(
                CHART_Q, self._chart_vars("Which store is biggest?"), self.client_user
            )
            again = await self._run(
                CHART_Q, self._chart_vars("which store is biggest?"), self.client_user
            )
        assert data["chartAnswer"] == {
            "ok": True,
            "answered": True,
            "answer": "Costco leads with 1,000 consumers sampled.",
            "reason": None,
        }
        assert again == data
        assert generate.call_count == 1, "the same question is read from the cache"
        prompt = generate.call_args[0][1]
        assert "- Costco: rank 1, 1,000" in prompt
        assert "- Kroger: rank 2, 400" in prompt
        assert "QUESTION: Which store is biggest?" in prompt
        for hidden in ("7,777", "9,999", "6,666", "Rivera"):
            assert hidden not in prompt, hidden

    @pytest.mark.asyncio
    async def test_a_made_up_number_in_a_chart_answer_becomes_the_plain_summary(self):
        await sync_to_async(self._seed_growth)(archived=False)
        reply = {"answerable": True, "answer": "Costco is 2.5 times Kroger."}
        with patch(GENERATE, return_value=reply):
            data = await self._run(
                CHART_Q, self._chart_vars("How much bigger is Costco?"), self.client_user
            )
        out = data["chartAnswer"]
        assert out["ok"] is True and out["answered"] is False
        assert out["answer"] == (
            "This chart shows 1,400 consumers sampled across 2 stores. "
            "The highest is Costco with 1,000; the lowest is Kroger with 400."
        )

    @pytest.mark.asyncio
    async def test_a_question_the_chart_cannot_answer_says_so(self):
        from recaps.chart_questions import CANNOT_ANSWER

        await sync_to_async(self._seed_growth)(archived=False)
        with patch(GENERATE, return_value={"answerable": False, "answer": ""}):
            data = await self._run(
                CHART_Q, self._chart_vars("Why did Texas grow?"), self.client_user
            )
        assert data["chartAnswer"]["answer"] == CANNOT_ANSWER
        assert "Ask AI" in CANNOT_ANSWER
        assert data["chartAnswer"]["answered"] is False

    @pytest.mark.asyncio
    async def test_a_chart_question_still_gets_a_reply_with_the_model_off(self):
        await sync_to_async(self._seed_growth)(archived=False)
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            data = await self._run(
                CHART_Q,
                self._chart_vars("Which week was best?", chart="consumers_by_week"),
                self.client_user,
            )
        out = data["chartAnswer"]
        assert out["ok"] is True and out["answered"] is False
        assert out["answer"].startswith("This chart shows 1,400 consumers sampled across")

    @pytest.mark.asyncio
    async def test_chart_questions_are_limited_per_person(self):
        from recaps import chart_questions

        await sync_to_async(self._seed_growth)(archived=False)
        reply = {"answerable": True, "answer": "Costco has 1,000."}
        with patch(GENERATE, return_value=reply) as generate, patch.object(
            chart_questions, "QUESTIONS_PER_HOUR", 2
        ):
            for question in ("One?", "Two?"):
                data = await self._run(
                    CHART_Q, self._chart_vars(question), self.client_user
                )
                assert data["chartAnswer"]["ok"] is True
            blocked = await self._run(
                CHART_Q, self._chart_vars("Three?"), self.client_user
            )
            # An answer already stored is still served.
            repeat = await self._run(CHART_Q, self._chart_vars("One?"), self.client_user)
            # Someone else has their own allowance.
            other = await self._run(CHART_Q, self._chart_vars("Three?"), self.admin)
        assert blocked["chartAnswer"] == {
            "ok": False,
            "answered": False,
            "answer": "",
            "reason": chart_questions.RATE_LIMITED,
        }
        assert repeat["chartAnswer"]["ok"] is True
        assert other["chartAnswer"]["ok"] is True
        assert generate.call_count == 3

    @pytest.mark.asyncio
    async def test_a_brand_user_asking_about_another_brands_chart_gets_their_own(self):
        await sync_to_async(self._seed_growth)(archived=False)
        reply = {"answerable": True, "answer": "Costco has 1,000."}
        with patch(GENERATE, return_value=reply) as generate:
            await self._run(
                CHART_Q, self._chart_vars("Biggest?", tenant=self.other), self.client_user
            )
        prompt = generate.call_args[0][1]
        assert "- Costco: rank 1, 1,000" in prompt
        assert "6,666" not in prompt

    @pytest.mark.asyncio
    async def test_an_unknown_chart_is_refused_without_asking_the_model(self):
        with patch(GENERATE) as generate:
            data = await self._run(
                CHART_Q, self._chart_vars("Anything?", chart="payroll"), self.client_user
            )
        assert data["chartAnswer"]["ok"] is False
        generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_chart_answer_reads_the_same_figures_the_chart_draws(self):
        """The chart's own query and the facts behind an answer about it
        come from one function, so they cannot differ. (That function still
        counts an approved recap that was later archived, as the KPI tile
        does: 1,000 + 8,888 here.)"""
        await sync_to_async(self._seed_growth)()
        chart = await self._run(
            """
            query B($tenantId: ID!, $startDate: String, $endDate: String) {
              tenantConsumerBreakdown(
                tenantId: $tenantId, startDate: $startDate, endDate: $endDate
              ) { total byStore { label kind consumers } }
            }
            """,
            self._window(),
            self.client_user,
        )
        drawn = chart["tenantConsumerBreakdown"]
        with patch(GENERATE, return_value={"answerable": False, "answer": ""}) as generate:
            await self._run(CHART_Q, self._chart_vars("Biggest?"), self.client_user)
        prompt = generate.call_args[0][1]
        assert f"TOTAL consumers sampled: {drawn['total']:,}." in prompt
        for row in drawn["byStore"]:
            assert f"- {row['label']}: " in prompt
            assert f"{row['consumers']:,}, " in prompt

    # ── where to sample next ──────────────────────────────────────────

    def _seed_sample_next(self):
        """Two Houston stores with results: one visited last week, one not
        for ten weeks. An account list with one store nobody has visited."""
        request_type = event_models.RequestType.objects.create(
            name="Retail", tenant=self.tenant, created_by=self.sys
        )

        def request(address, retailer):
            return event_models.Request.objects.create(
                name="Gig", address=address, tenant=self.tenant, retailer=retailer,
                request_type=request_type, created_by=self.sys,
            )

        heb = self._retailer("HEB")
        old = request("10 Old Rd, Houston, TX 77002", heb)
        fresh = request("20 New Rd, Houston, TX 77003", self.kroger)
        for i in range(6):
            self._recap(
                70 + i, 100, 30, retailer=heb, state=self.tx,
                address="10 Old Rd, Houston, TX 77002", request=old,
            )
            self._recap(
                3 + i, 100, 10, retailer=self.kroger, state=self.tx,
                address="20 New Rd, Houston, TX 77003", request=fresh,
            )
        event_models.AccountListEntry.objects.create(
            tenant=self.tenant, name="Fiesta Mart", address="30 Never St",
            city="Houston", state="TX", zip="77004", dedupe_key="fiesta",
        )
        # Already a store with visits: must not be listed as never visited.
        event_models.AccountListEntry.objects.create(
            tenant=self.tenant, name="HEB Old Rd", address="10 Old Rd",
            city="Houston", state="TX", zip="77002", dedupe_key="heb-old",
        )

    @pytest.mark.asyncio
    async def test_staff_get_a_ranked_list_with_reasons(self):
        await sync_to_async(self._seed_sample_next)()
        with patch(GENERATE, side_effect=AiUnavailable("off")):
            data = await self._run(
                SAMPLE_NEXT_Q, {"tenantId": str(self.tenant.id)}, self.admin
            )
        items = data["tenantSampleNext"]["items"]
        assert [(i["name"], i["kind"]) for i in items] == [
            ("HEB", "revisit"),
            ("Fiesta Mart", "never_visited"),
        ]
        assert items[0]["reason"] == (
            "Best conversion in Houston at 30%, last visited 10 weeks ago."
        )
        assert items[1]["reason"] == (
            "Never visited. 2 stores in Houston averaged 20% conversion."
        )
        assert items[1]["market"] == "Houston, TX"

    @pytest.mark.asyncio
    async def test_a_brand_user_gets_nothing_until_the_switch_is_on(self):
        await sync_to_async(self._seed_sample_next)()
        variables = {"tenantId": str(self.tenant.id)}
        with patch(GENERATE) as generate:
            data = await self._run(SAMPLE_NEXT_Q, variables, self.client_user)
            assert data["tenantSampleNext"] is None
            generate.assert_not_called()
        with patch(GENERATE, side_effect=AiUnavailable("off")), override_settings(
            SAMPLE_NEXT_FOR_CLIENTS=True
        ):
            data = await self._run(SAMPLE_NEXT_Q, variables, self.client_user)
        assert len(data["tenantSampleNext"]["items"]) == 2
