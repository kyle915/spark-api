"""Coverage for the recap quality-check backend:

* :func:`recaps.recap_quality.recap_quality_flags` — the deterministic (+
  optional cached-AI) quality read for ONE recap, across BOTH shapes (legacy
  :class:`recaps.models.Recap` + custom :class:`recaps.models.CustomRecap`).
  Asserts the right flags + a sane score for a complete recap (high score, few
  flags) and for the failure modes (no photos, empty feedback, sold > sampled,
  all-zero, implausible/negative numbers, missing KPIs).
* The optional AI pass + its per-recap snapshot cache — the OpenAI call is
  always MOCKED (these tests never hit the network); a missing key / AI failure
  degrades to deterministic-only and never raises.
* The ``recapQualityFlags(recapId, isCustom)`` GraphQL query on the clients
  schema — the never-raises, tenant-scoped shell (clients pinned to their own
  tenant; admins may read any; out-of-scope / missing -> neutral 100).

Fixture style mirrors test_tenant_market_performance.py / test_tenant_
sentiment.py. Image-typed files are created with ``.jpg`` blobs (the ``isImage``
rule); a ``.pdf`` blob is used as a non-photo attachment.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Product, ProductType
from recaps import models as recap_models
from recaps.recap_quality import (
    MIN_EXPECTED_PHOTOS,
    recap_quality_flags,
)


def _codes(result: dict) -> set[str]:
    return {f["code"] for f in result["flags"]}


# ---------------------------------------------------------------------------
# Deterministic checks on legacy Recap.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
class TestLegacyRecapQuality(AmbassadorsGraphQLTestCase):
    """recap_quality_flags on a legacy Recap: each failure mode + the happy path."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.event = self.create_event(name="Demo @ Store", tenant=self.tenant)
        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )

    # -- builders -------------------------------------------------------

    def _recap(self, **kwargs) -> recap_models.Recap:
        defaults = dict(
            name="legacy recap",
            event=self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        defaults.update(kwargs)
        return recap_models.Recap.objects.create(**defaults)

    def _file(self, recap, blob: str):
        return recap_models.RecapFile.objects.create(
            name=blob,
            file=blob,
            file_type=self.file_type,
            recap=recap,
            created_by=self.system_user,
        )

    def _feedback(self, recap, **kwargs):
        return recap_models.ConsumerFeedback.objects.create(
            recap=recap,
            created_by=self.system_user,
            updated_by=self.system_user,
            **kwargs,
        )

    def _engagements(self, recap, total_consumer):
        return recap_models.ConsumerEngagements.objects.create(
            recap=recap,
            total_consumer=total_consumer,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _samples(self, recap, quantity):
        product_type = ProductType.objects.create(
            name="Beverage", tenant=self.tenant, created_by=self.system_user
        )
        product = Product.objects.create(
            name="La Croix",
            product_type=product_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        return recap_models.ProductSamples.objects.create(
            recap=recap,
            product=product,
            quantity=quantity,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

    def _complete_recap(self) -> recap_models.Recap:
        """A solid recap: 2 photos, real feedback, consistent numbers."""
        recap = self._recap(
            total_engagements=200,
            products_sold=15,
            total_cans_sold=10,
            total_packs_sold=5,
        )
        self._file(recap, "recap_files/a/photo1.jpg")
        self._file(recap, "recap_files/a/photo2.jpg")
        self._feedback(
            recap,
            quotes="Customers raved about the grapefruit flavor all afternoon.",
            positive_stories="A regular bought a full case after sampling.",
        )
        self._engagements(recap, total_consumer=150)
        self._samples(recap, quantity=120)
        return recap

    # -- happy path -----------------------------------------------------

    def test_complete_recap_scores_high_with_no_flags(self):
        recap = self._complete_recap()
        result = recap_quality_flags(recap.id)
        assert result["flags"] == []
        assert result["score"] == 100

    # -- photos ---------------------------------------------------------

    def test_no_photos_high_severity(self):
        recap = self._complete_recap()
        recap_models.RecapFile.objects.filter(recap=recap).delete()
        result = recap_quality_flags(recap.id)
        assert "no_photos" in _codes(result)
        flag = next(f for f in result["flags"] if f["code"] == "no_photos")
        assert flag["severity"] == "high"
        assert result["score"] < 100

    def test_few_photos_low_severity(self):
        recap = self._complete_recap()
        # Drop to a single photo (< MIN_EXPECTED_PHOTOS) -> low-severity nudge.
        assert MIN_EXPECTED_PHOTOS >= 2
        recap_models.RecapFile.objects.filter(recap=recap).first().delete()
        result = recap_quality_flags(recap.id)
        assert "few_photos" in _codes(result)
        flag = next(f for f in result["flags"] if f["code"] == "few_photos")
        assert flag["severity"] == "low"

    def test_pdf_attachment_does_not_count_as_photo(self):
        recap = self._recap(total_engagements=10)
        # A PDF is not an image -> still reads as zero photos (no_photos).
        self._file(recap, "recap_files/a/report.pdf")
        result = recap_quality_flags(recap.id)
        assert "no_photos" in _codes(result)

    # -- feedback -------------------------------------------------------

    def test_no_feedback_medium_severity(self):
        recap = self._complete_recap()
        recap_models.ConsumerFeedback.objects.filter(recap=recap).delete()
        result = recap_quality_flags(recap.id)
        assert "no_feedback" in _codes(result)
        flag = next(f for f in result["flags"] if f["code"] == "no_feedback")
        assert flag["severity"] == "medium"

    def test_blank_feedback_counts_as_missing(self):
        recap = self._complete_recap()
        recap_models.ConsumerFeedback.objects.filter(recap=recap).delete()
        # Only whitespace / empty columns -> still "no feedback".
        self._feedback(recap, quotes="   ", feedback="")
        result = recap_quality_flags(recap.id)
        assert "no_feedback" in _codes(result)

    # -- numeric inconsistencies ----------------------------------------

    def test_sold_exceeds_sampled(self):
        recap = self._recap(
            total_engagements=50,
            products_sold=500,  # more sold than sampled
        )
        self._file(recap, "p1.jpg")
        self._file(recap, "p2.jpg")
        self._feedback(recap, quotes="Great event.")
        self._engagements(recap, total_consumer=40)
        self._samples(recap, quantity=100)  # only 100 sampled
        result = recap_quality_flags(recap.id)
        assert "sold_exceeds_sampled" in _codes(result)
        flag = next(
            f for f in result["flags"] if f["code"] == "sold_exceeds_sampled"
        )
        assert flag["severity"] == "medium"

    def test_all_zero_kpis(self):
        recap = self._recap(
            total_engagements=0,
            products_sold=0,
            total_cans_sold=0,
            total_packs_sold=0,
        )
        self._file(recap, "p1.jpg")
        self._file(recap, "p2.jpg")
        self._feedback(recap, quotes="Quiet day, slow foot traffic.")
        self._engagements(recap, total_consumer=0)
        self._samples(recap, quantity=0)
        result = recap_quality_flags(recap.id)
        assert "all_zero_kpis" in _codes(result)

    def test_no_kpis_at_all_high_severity(self):
        # No typed numbers, no engagement/sample rows -> the "empty report"
        # high-severity flag (distinct from all-zero).
        recap = self._recap()
        self._file(recap, "p1.jpg")
        self._file(recap, "p2.jpg")
        self._feedback(recap, quotes="Some real feedback here.")
        result = recap_quality_flags(recap.id)
        assert "no_kpis" in _codes(result)
        flag = next(f for f in result["flags"] if f["code"] == "no_kpis")
        assert flag["severity"] == "high"

    def test_implausible_value(self):
        recap = self._recap(total_engagements=10_000_000)  # absurd
        self._file(recap, "p1.jpg")
        self._file(recap, "p2.jpg")
        self._feedback(recap, quotes="Busy but believable on the ground.")
        result = recap_quality_flags(recap.id)
        assert "implausible_value" in _codes(result)

    def test_negative_value(self):
        recap = self._recap(total_engagements=-5)
        self._file(recap, "p1.jpg")
        self._file(recap, "p2.jpg")
        self._feedback(recap, quotes="Data entry was rushed.")
        result = recap_quality_flags(recap.id)
        assert "negative_value" in _codes(result)

    # -- scoring + missing ----------------------------------------------

    def test_score_decreases_with_more_flags(self):
        # An empty-ish recap (no photos + no feedback + no kpis) scores well
        # below a recap with a single low-severity nit.
        bad = self._recap()
        bad_result = recap_quality_flags(bad.id)
        assert bad_result["score"] < 50
        assert len(bad_result["flags"]) >= 2

    def test_missing_recap_returns_neutral_100(self):
        result = recap_quality_flags(999_999_999)
        assert result == {"score": 100, "flags": []}

    def test_garbage_id_returns_neutral_100(self):
        assert recap_quality_flags("not-an-int") == {"score": 100, "flags": []}

    def test_flags_sorted_worst_first(self):
        recap = self._recap()  # triggers high (no_photos, no_kpis) + medium
        result = recap_quality_flags(recap.id)
        ranks = {"high": 0, "medium": 1, "low": 2}
        severities = [ranks[f["severity"]] for f in result["flags"]]
        assert severities == sorted(severities)


# ---------------------------------------------------------------------------
# Deterministic checks on custom CustomRecap.
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
class TestCustomRecapQuality(AmbassadorsGraphQLTestCase):
    """recap_quality_flags on a CustomRecap (free-text KPIs + feedback)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Liquid Death")
        self.event = self.create_event(name="WF Demo", tenant=self.tenant)
        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="LD Template",
            event_type=self.event_type,
            tenant=self.tenant,
            created_by=self.system_user,
        )
        self.field_type = recap_models.CustomRecapFieldType.objects.create(
            name="text", created_by=self.system_user
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Section", tenant=self.tenant, created_by=self.system_user
        )

    # -- builders -------------------------------------------------------

    def _recap(self, **kwargs) -> recap_models.CustomRecap:
        defaults = dict(
            name="custom recap",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        defaults.update(kwargs)
        return recap_models.CustomRecap.objects.create(**defaults)

    def _field(self, name: str) -> recap_models.CustomField:
        return recap_models.CustomField.objects.create(
            name=name,
            custom_recap_template=self.template,
            custom_field_type=self.field_type,
            recap_section=self.section,
            created_by=self.system_user,
        )

    def _value(self, recap, field_name: str, value: str):
        field = self._field(field_name)
        return recap_models.CustomFieldValue.objects.create(
            custom_recap=recap,
            custom_field=field,
            value=value,
            created_by=self.system_user,
        )

    def _file(self, recap, blob: str):
        return recap_models.CustomRecapFile.objects.create(
            name=blob,
            url=blob,
            file_type=self.file_type,
            custom_recap=recap,
            created_by=self.system_user,
        )

    def _complete_recap(self) -> recap_models.CustomRecap:
        recap = self._recap(total_engagements=120)
        self._file(recap, "recap_files/c/photo1.jpg")
        self._file(recap, "recap_files/c/photo2.jpg")
        self._value(recap, "Consumer quotes", "Loved the cans, very refreshing.")
        self._value(recap, "Total consumers sampled", "80")
        self._value(recap, "Cans Sold", "12")
        return recap

    # -- tests ----------------------------------------------------------

    def test_complete_custom_recap_high_score(self):
        recap = self._complete_recap()
        result = recap_quality_flags(recap.id, is_custom=True)
        assert result["flags"] == []
        assert result["score"] == 100

    def test_custom_no_photos(self):
        recap = self._complete_recap()
        recap_models.CustomRecapFile.objects.filter(
            custom_recap=recap
        ).delete()
        result = recap_quality_flags(recap.id, is_custom=True)
        assert "no_photos" in _codes(result)

    def test_custom_no_feedback(self):
        # A KPI-only custom recap (no feedback-named field) -> no_feedback.
        recap = self._recap(total_engagements=50)
        self._file(recap, "p1.jpg")
        self._file(recap, "p2.jpg")
        self._value(recap, "Total consumers sampled", "40")
        result = recap_quality_flags(recap.id, is_custom=True)
        assert "no_feedback" in _codes(result)

    def test_custom_feedback_detected_by_field_name(self):
        recap = self._recap(total_engagements=50)
        self._file(recap, "p1.jpg")
        self._file(recap, "p2.jpg")
        self._value(recap, "Total consumers sampled", "40")
        self._value(recap, "Consumer feedback", "People asked where to buy it.")
        result = recap_quality_flags(recap.id, is_custom=True)
        assert "no_feedback" not in _codes(result)

    def test_custom_sold_exceeds_sampled(self):
        recap = self._recap(total_engagements=50)
        self._file(recap, "p1.jpg")
        self._file(recap, "p2.jpg")
        self._value(recap, "Consumer quotes", "Quick chats, lots of interest.")
        self._value(recap, "Total consumers sampled", "20")  # samples = 20
        self._value(recap, "Cans Sold", "300")  # sold 300 -> impossible
        result = recap_quality_flags(recap.id, is_custom=True)
        assert "sold_exceeds_sampled" in _codes(result)

    def test_custom_missing_recap_neutral(self):
        assert recap_quality_flags(987654321, is_custom=True) == {
            "score": 100,
            "flags": [],
        }

    def test_custom_wrong_shape_flag_isolation(self):
        # Asking for a custom id as a LEGACY recap (is_custom=False) must not
        # find it -> neutral, not a crash / wrong flags.
        recap = self._complete_recap()
        assert recap_quality_flags(recap.id, is_custom=False) == {
            "score": 100,
            "flags": [],
        }


# ---------------------------------------------------------------------------
# Optional AI pass + per-recap snapshot cache (OpenAI mocked).
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
class TestRecapQualityAiPass(AmbassadorsGraphQLTestCase):
    """The cached AI low-quality pass: mocked happy path, caching, degrade."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.event = self.create_event(name="Demo", tenant=self.tenant)
        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )

    def _recap_with_feedback(self, text="some feedback text here"):
        recap = recap_models.Recap.objects.create(
            name="r",
            event=self.event,
            total_engagements=100,
            products_sold=5,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.RecapFile.objects.create(
            name="p1.jpg", file="p1.jpg", file_type=self.file_type,
            recap=recap, created_by=self.system_user,
        )
        recap_models.RecapFile.objects.create(
            name="p2.jpg", file="p2.jpg", file_type=self.file_type,
            recap=recap, created_by=self.system_user,
        )
        recap_models.ConsumerEngagements.objects.create(
            recap=recap, total_consumer=80,
            created_by=self.system_user, updated_by=self.system_user,
        )
        recap_models.ConsumerFeedback.objects.create(
            recap=recap, quotes=text,
            created_by=self.system_user, updated_by=self.system_user,
        )
        return recap

    def test_ai_low_quality_adds_flag_and_lowers_score(self):
        recap = self._recap_with_feedback("good")
        payload = {"low_quality": True, "reason": "too generic"}
        with patch(
            "utils.ai_text.generate_json", return_value=payload
        ) as mock_ai:
            result = recap_quality_flags(recap.id)
        mock_ai.assert_called_once()
        assert "schema" in mock_ai.call_args.kwargs
        assert "low_feedback_quality" in _codes(result)
        flag = next(
            f for f in result["flags"] if f["code"] == "low_feedback_quality"
        )
        assert flag["severity"] == "low"
        assert "too generic" in flag["label"]
        assert result["score"] < 100

    def test_ai_good_verdict_adds_no_flag(self):
        recap = self._recap_with_feedback(
            "Specific, detailed account of how consumers reacted."
        )
        payload = {"low_quality": False, "reason": ""}
        with patch("utils.ai_text.generate_json", return_value=payload):
            result = recap_quality_flags(recap.id)
        assert "low_feedback_quality" not in _codes(result)
        # A complete recap with good feedback stays clean.
        assert result["score"] == 100

    def test_ai_result_is_cached_single_call(self):
        recap = self._recap_with_feedback("good")
        payload = {"low_quality": True, "reason": "thin"}
        with patch(
            "utils.ai_text.generate_json", return_value=payload
        ) as mock_ai:
            recap_quality_flags(recap.id)
            recap_quality_flags(recap.id)  # second read
        # AI asked exactly once; second read served from the snapshot.
        mock_ai.assert_called_once()
        assert recap_models.RecapQualitySnapshot.objects.filter(
            recap_id=recap.id, is_custom=False
        ).count() == 1

    def test_negative_verdict_also_cached_no_reask(self):
        recap = self._recap_with_feedback("Detailed and specific feedback.")
        payload = {"low_quality": False, "reason": ""}
        with patch(
            "utils.ai_text.generate_json", return_value=payload
        ) as mock_ai:
            recap_quality_flags(recap.id)
            recap_quality_flags(recap.id)
        mock_ai.assert_called_once()  # negative verdict stored, not re-asked
        snap = recap_models.RecapQualitySnapshot.objects.get(
            recap_id=recap.id, is_custom=False
        )
        assert snap.low_quality is False

    def test_ai_failure_degrades_to_deterministic_only(self):
        recap = self._recap_with_feedback("good")
        # generate_json returns None on any mid-flight failure.
        with patch("utils.ai_text.generate_json", return_value=None):
            result = recap_quality_flags(recap.id)
        assert "low_feedback_quality" not in _codes(result)
        # Deterministic checks still pass -> clean recap stays 100.
        assert result["score"] == 100

    def test_ai_unavailable_exception_never_raises(self):
        recap = self._recap_with_feedback("good")
        from utils.ai_text import AiUnavailable

        with patch(
            "utils.ai_text.generate_json", side_effect=AiUnavailable("no key")
        ):
            result = recap_quality_flags(recap.id)
        assert "low_feedback_quality" not in _codes(result)

    def test_ai_not_called_when_no_feedback(self):
        # No feedback -> the deterministic no_feedback flag fires and the AI is
        # never invoked (nothing to judge).
        recap = recap_models.Recap.objects.create(
            name="r", event=self.event, total_engagements=10,
            created_by=self.system_user, updated_by=self.system_user,
        )
        recap_models.RecapFile.objects.create(
            name="p1.jpg", file="p1.jpg", file_type=self.file_type,
            recap=recap, created_by=self.system_user,
        )
        recap_models.RecapFile.objects.create(
            name="p2.jpg", file="p2.jpg", file_type=self.file_type,
            recap=recap, created_by=self.system_user,
        )
        with patch("utils.ai_text.generate_json") as mock_ai:
            result = recap_quality_flags(recap.id)
        mock_ai.assert_not_called()
        assert "no_feedback" in _codes(result)


# ---------------------------------------------------------------------------
# GraphQL resolver: shape, tenant scoping, never-raise.
# ---------------------------------------------------------------------------
QUALITY_QUERY = """
query Quality($recapId: ID!, $isCustom: Boolean) {
  recapQualityFlags(recapId: $recapId, isCustom: $isCustom) {
    score
    flags {
      code
      label
      severity
    }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestRecapQualityResolver(AmbassadorsGraphQLTestCase):
    """recapQualityFlags GraphQL: scoping (client pinned / admin any) + degrade."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Liquid Death")

        self.spark_admin = self.create_user(
            username="admin-quality",
            email="admin-quality@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="client-quality",
            email="client-quality@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        self.file_type = FileType.objects.create(
            name="image", created_by=self.system_user
        )

        # Our tenant's legacy recap: NO photos -> a high-severity flag.
        self.event = self.create_event(name="Demo", tenant=self.tenant)
        self.recap = recap_models.Recap.objects.create(
            name="ours",
            event=self.event,
            total_engagements=10,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ConsumerFeedback.objects.create(
            recap=self.recap, quotes="Real feedback.",
            created_by=self.system_user, updated_by=self.system_user,
        )

        # Other tenant's recap (for the cross-tenant isolation test).
        self.other_event = self.create_event(
            name="Theirs", tenant=self.other_tenant
        )
        self.other_recap = recap_models.Recap.objects.create(
            name="theirs",
            event=self.other_event,
            total_engagements=5,
            created_by=self.system_user,
            updated_by=self.system_user,
        )

        # Our tenant's custom recap, fully complete -> clean 100.
        self.event_type = self.create_event_type(
            name="Sampling", tenant=self.tenant
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="GB Template", event_type=self.event_type,
            tenant=self.tenant, created_by=self.system_user,
        )
        self.custom_recap = recap_models.CustomRecap.objects.create(
            name="ours custom", event=self.event, tenant=self.tenant,
            custom_recap_template=self.template, total_engagements=120,
            created_by=self.system_user, updated_by=self.system_user,
        )
        recap_models.CustomRecapFile.objects.create(
            name="c1.jpg", url="c1.jpg", file_type=self.file_type,
            custom_recap=self.custom_recap, created_by=self.system_user,
        )
        recap_models.CustomRecapFile.objects.create(
            name="c2.jpg", url="c2.jpg", file_type=self.file_type,
            custom_recap=self.custom_recap, created_by=self.system_user,
        )
        ft = recap_models.CustomRecapFieldType.objects.create(
            name="text", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="S", tenant=self.tenant, created_by=self.system_user
        )
        field = recap_models.CustomField.objects.create(
            name="Consumer quotes", custom_recap_template=self.template,
            custom_field_type=ft, recap_section=section,
            created_by=self.system_user,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=self.custom_recap, custom_field=field,
            value="People loved it.", created_by=self.system_user,
        )
        field2 = recap_models.CustomField.objects.create(
            name="Total consumers sampled", custom_recap_template=self.template,
            custom_field_type=ft, recap_section=section,
            created_by=self.system_user,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=self.custom_recap, custom_field=field2,
            value="80", created_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_admin_reads_legacy_recap(self):
        result = await self._execute_query_authenticated(
            QUALITY_QUERY,
            {"recapId": str(self.recap.id), "isCustom": False},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        data = result.data["recapQualityFlags"]
        codes = {f["code"] for f in data["flags"]}
        assert "no_photos" in codes
        assert data["score"] < 100

    @pytest.mark.asyncio
    async def test_admin_reads_custom_recap(self):
        result = await self._execute_query_authenticated(
            QUALITY_QUERY,
            {"recapId": str(self.custom_recap.id), "isCustom": True},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        data = result.data["recapQualityFlags"]
        assert data["score"] == 100
        assert data["flags"] == []

    @pytest.mark.asyncio
    async def test_client_reads_own_recap(self):
        result = await self._execute_query_authenticated(
            QUALITY_QUERY,
            {"recapId": str(self.recap.id), "isCustom": False},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        data = result.data["recapQualityFlags"]
        assert "no_photos" in {f["code"] for f in data["flags"]}

    @pytest.mark.asyncio
    async def test_client_cannot_read_other_tenant_recap(self):
        # Client asks about the OTHER tenant's recap -> neutral 100, no leak.
        result = await self._execute_query_authenticated(
            QUALITY_QUERY,
            {"recapId": str(self.other_recap.id), "isCustom": False},
            self.client_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        data = result.data["recapQualityFlags"]
        assert data == {"score": 100, "flags": []}

    @pytest.mark.asyncio
    async def test_missing_recap_is_neutral_not_error(self):
        result = await self._execute_query_authenticated(
            QUALITY_QUERY,
            {"recapId": "987654321", "isCustom": False},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert result.data["recapQualityFlags"] == {"score": 100, "flags": []}

    @pytest.mark.asyncio
    async def test_default_is_custom_false(self):
        # Omitting isCustom defaults to the legacy shape.
        result = await self._execute_query_authenticated(
            """
            query Q($recapId: ID!) {
              recapQualityFlags(recapId: $recapId) { score flags { code } }
            }
            """,
            {"recapId": str(self.recap.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        assert "no_photos" in {
            f["code"] for f in result.data["recapQualityFlags"]["flags"]
        }
