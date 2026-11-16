"""The reviewer's one-line "why was this flagged".

  * one flag is a fixed sentence — the model is not called;
  * several flags are joined by the model, which is handed flag codes and
    numbers only (no ambassador, store or brand name, no typed answer);
  * the line is cached per recap + flags hash, so a page load does not call
    the model again, and is rewritten when the flags change;
  * a sentence holding a number the model was not given is thrown away;
  * with no model the page gets no line and shows its checks as before;
  * a brand user never gets one.

The model is never called: ``utils.ai_text._post_json`` is mocked.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings
from django.utils import timezone

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import flag_explanation as fx
from recaps import models as recap_models

EXPLAIN = """
query Explain($recapId: ID!, $isCustom: Boolean!) {
  recapFlagExplanation(recapId: $recapId, isCustom: $isCustom) {
    summary
    source
    flagCount
  }
}
"""


def _reply(why: str) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"why": why})}}]}


# ─── Pure ───────────────────────────────────────────────────────────


def test_one_flag_is_a_fixed_sentence_with_what_to_check():
    signals = [{"code": "sold_exceeds_sampled", "numbers": {"sold": 55, "sampled": 40}}]
    assert fx.single_flag_line(signals) == (
        "Sold 55 against 40 samples handed out. Check the sold count first."
    )


def test_photo_reuse_names_the_other_recaps_date_not_a_person():
    signals = [
        {"code": "photo_reuse", "numbers": {"photos": 2, "other_recaps": 1, "date": "Oct 12"}}
    ]
    assert fx.single_flag_line(signals) == (
        "2 photos also appear on the Oct 12 recap. Compare the photos first."
    )


def test_a_flag_without_numbers_still_reads_plainly():
    assert fx.single_flag_line([{"code": "no_photos", "numbers": {}}]) == (
        "There are no photos. Ask for photos first."
    )
    assert fx.single_flag_line([{"code": "some_new_code", "numbers": {}}]) == (
        "A check was flagged. Check that first."
    )


def test_numbers_are_read_from_the_checks_fixed_wording():
    assert fx._check_numbers(
        "engagements_rate",
        "1,400 engagements in a 4h shift (~350/hr) — check for a typo",
        "",
    ) == {"count": "1,400", "kind": "engagements", "hours": "4", "per_hour": "350"}
    assert fx._check_numbers(
        "gps_distance", "Clock-in 3.2 mi from the venue — worth a look", ""
    ) == {"miles": "3.2"}
    assert fx._check_numbers(
        "photo_buckets", "Missing photo bucket: Receipt", "2/3 required buckets have photos"
    ) == {"missing": 1, "required": 3}
    assert fx._check_numbers(
        "receipt_total_mismatch", "$80.00 entered, receipts total $52.10", ""
    ) == {"entered": "80.00", "receipts": "52.10"}
    assert fx._check_numbers(
        "receipt_total_mismatch", "Receipts total $90.00, more than the $60.00 entered", ""
    ) == {"receipts": "90.00", "entered": "60.00"}


def test_a_model_sentence_with_a_number_it_was_not_given_is_dropped():
    signals = [
        {"code": "sold_exceeds_sampled", "numbers": {"sold": 55, "sampled": 40}},
        {"code": "photo_reuse", "numbers": {"photos": 2, "other_recaps": 1, "date": "Oct 12"}},
    ]
    ok = {"why": "Sold 55 against 40 handed out, and two photos also appear on the Oct 12 recap"}
    assert fx._clean_model_line(ok, signals) == (
        "Sold 55 against 40 handed out, and two photos also appear on the Oct 12 recap."
    )
    assert fx._clean_model_line({"why": "Sold 55 against 40, a 138% rate"}, signals) is None
    assert fx._clean_model_line({"why": "Sold 55 against 40 and three photos repeat"}, signals) is None
    assert fx._clean_model_line({"why": ""}, signals) is None
    assert fx._clean_model_line({"why": "x" * 400}, signals) is None
    assert fx._clean_model_line(None, signals) is None


def test_the_hash_follows_the_codes_and_numbers():
    a = [{"code": "sold_exceeds_sampled", "numbers": {"sold": 55, "sampled": 40}}]
    b = [{"code": "sold_exceeds_sampled", "numbers": {"sold": 56, "sampled": 40}}]
    assert fx.signals_hash(a) == fx.signals_hash(json.loads(json.dumps(a)))
    assert fx.signals_hash(a) != fx.signals_hash(b)


# ─── With a database ────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestFlagExplanation(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Alpha Brand")
        self.admin = self.create_user(
            username="fx-admin", email="fx-admin@test.com", role=self.roles["spark_admin"]
        )
        self.client_user = self.create_user(
            username="fx-client", email="fx-client@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        credited = self.create_user(
            username="jordan-rivera-fx",
            email="jordan.rivera.fx@ba.test",
            role=self.roles["ambassador"],
        )
        credited.first_name = "Jordan"
        credited.last_name = "Rivera"
        credited.save()
        self.ba = self.create_ambassador(credited)
        self.image_type = FileType.objects.create(name="image", created_by=self.system_user)

        now = timezone.now()
        self.event = self.create_event(
            name="Alpha Store One",
            tenant=self.tenant,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )

    # ── fixtures ──────────────────────────────────────────────────────

    def _recap(self, *, sold, sampled, photos, feedback, approved=False):
        """A legacy recap. ``sold > sampled`` raises sold_exceeds_sampled,
        ``photos == 0`` raises no_photos, ``feedback=None`` raises no_feedback."""
        product_type = event_models.ProductType.objects.create(
            name="Beverage", tenant=self.tenant, created_by=self.system_user
        )
        product = event_models.Product.objects.create(
            name="Alpha Can",
            product_type=product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        recap = recap_models.Recap.objects.create(
            name="Alpha recap",
            event=self.event,
            total_engagements=60,
            products_sold=sold,
            approved=approved,
            submited_at=timezone.now(),
            ambassador=self.ba,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ProductSamples.objects.create(
            recap=recap,
            product=product,
            quantity=sampled,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        if feedback:
            recap_models.ConsumerFeedback.objects.create(
                recap=recap, feedback=feedback, quotes=feedback
            )
        for i in range(photos):
            recap_models.RecapFile.objects.create(
                name=f"photo-{i}.jpg",
                file=f"recap_files/fx-{recap.id}-{i}.jpg",
                recap=recap,
                file_type=self.image_type,
                created_by=self.system_user,
            )
        return recap

    @sync_to_async
    def _make(self, **kwargs):
        return self._recap(**kwargs)

    async def _explain(self, user, recap):
        result = await self._execute_query_authenticated(
            EXPLAIN, {"recapId": str(recap.id), "isCustom": False}, user, self.endpoint_path
        )
        assert result.errors is None, result.errors
        return result.data["recapFlagExplanation"]

    # ── tests ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_nothing_flagged_is_no_line(self):
        recap = await self._make(
            sold=10, sampled=40, photos=2, feedback="Shoppers liked the peach one best."
        )
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json"
        ) as post:
            assert await self._explain(self.admin, recap) is None
        post.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_flag_never_calls_the_model(self):
        recap = await self._make(
            sold=55, sampled=40, photos=2, feedback="Shoppers liked the peach one best."
        )
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json"
        ) as post:
            payload = await self._explain(self.admin, recap)
        post.assert_not_called()
        assert payload == {
            "summary": "Sold 55 against 40 samples handed out. Check the sold count first.",
            "source": "rules",
            "flagCount": 1,
        }

    @pytest.mark.asyncio
    async def test_several_flags_are_joined_once_and_cached(self):
        recap = await self._make(
            sold=55, sampled=40, photos=0, feedback="Jordan Rivera says SECRET FEEDBACK."
        )
        why = "Sold 55 against 40 samples handed out, and there are no photos"
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json", return_value=_reply(why)
        ) as post:
            first = await self._explain(self.admin, recap)
            second = await self._explain(self.admin, recap)

        assert post.call_count == 1, "the second page load must read the cache"
        assert first == second == {
            "summary": f"{why}. Check the sold count first.",
            "source": "model",
            "flagCount": 2,
        }
        sent = json.dumps(post.call_args[0][1])
        # Codes and numbers went out…
        assert "sold_exceeds_sampled" in sent and "no_photos" in sent
        assert "55" in sent and "40" in sent
        # …and nothing a person typed, and no name.
        for secret in ("Jordan", "Rivera", "SECRET FEEDBACK", "Alpha Brand", "Alpha Store One"):
            assert secret not in sent

    @pytest.mark.asyncio
    async def test_the_line_is_rewritten_when_the_flags_change(self):
        recap = await self._make(sold=55, sampled=40, photos=0, feedback="Fine day.")
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json",
            return_value=_reply("Sold 55 against 40 samples handed out, and there are no photos"),
        ) as post:
            await self._explain(self.admin, recap)

            @sync_to_async
            def _sell_more():
                recap_models.Recap.objects.filter(id=recap.id).update(products_sold=70)

            await _sell_more()
            post.return_value = _reply(
                "Sold 70 against 40 samples handed out, and there are no photos"
            )
            payload = await self._explain(self.admin, recap)
        assert post.call_count == 2
        assert payload["summary"].startswith("Sold 70 against 40")

    @pytest.mark.asyncio
    async def test_an_invented_number_is_not_shown_and_not_asked_for_again(self):
        recap = await self._make(sold=55, sampled=40, photos=0, feedback="Fine day.")
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json",
            return_value=_reply("Sold 55 against 40, a 138 percent rate, with no photos"),
        ) as post:
            first = await self._explain(self.admin, recap)
            second = await self._explain(self.admin, recap)
        assert post.call_count == 1
        assert first == second == {"summary": None, "source": "rules", "flagCount": 2}

    @pytest.mark.asyncio
    async def test_no_model_gives_no_line_so_the_page_shows_its_checks(self):
        recap = await self._make(sold=55, sampled=40, photos=0, feedback="Fine day.")
        with override_settings(OPENAI_API_KEY=""), patch(
            "utils.ai_text._post_json"
        ) as post:
            payload = await self._explain(self.admin, recap)
        post.assert_not_called()
        assert payload == {"summary": None, "source": "rules", "flagCount": 2}

    @pytest.mark.asyncio
    async def test_an_approved_recap_does_not_spend_a_model_call(self):
        recap = await self._make(
            sold=55, sampled=40, photos=0, feedback="Fine day.", approved=True
        )
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json"
        ) as post:
            payload = await self._explain(self.admin, recap)
        post.assert_not_called()
        assert payload["summary"] is None and payload["flagCount"] == 2

    @pytest.mark.asyncio
    async def test_a_brand_user_never_gets_a_line(self):
        one = await self._make(
            sold=55, sampled=40, photos=2, feedback="Shoppers liked the peach one best."
        )
        with override_settings(OPENAI_API_KEY="test-key"), patch(
            "utils.ai_text._post_json", return_value=_reply("Sold 55 against 40")
        ) as post:
            # Their own tenant's recap, one flag (no model needed): still null.
            assert await self._explain(self.client_user, one) is None
            assert await self._explain(self.admin, one) is not None
        post.assert_not_called()

        @sync_to_async
        def _cache_rows():
            return recap_models.RecapAiReview.objects.count()

        assert await _cache_rows() == 0
