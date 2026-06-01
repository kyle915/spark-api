"""Coverage for the "What people are saying" consumer-sentiment backend:

* :func:`recaps.tenant_sentiment.gather_consumer_feedback` — collecting a
  tenant's free-text consumer feedback across BOTH recap shapes (legacy
  ``ConsumerFeedback`` columns through the event FK + custom-template
  ``CustomFieldValue`` rows whose field name reads like feedback), newest-first,
  deduped, and HARD-BOUNDED by both a snippet count and a cumulative-character
  budget, with the same calendar-year filter the other tenant aggregates use.
* :func:`recaps.tenant_sentiment.build_tenant_sentiment` + the defensive
  cleaner — too-little-data and AI-failure both degrade to ``None``, and a
  malformed AI payload is clamped/dropped (including the verbatim-quote guard
  that drops any quote not present in the gathered snippets). The OpenAI call is
  always MOCKED — these tests never hit the network.

Fixtures mirror the style of test_tenant_insights.py / test_tenant_market_
performance.py: because the relevant ``created_at`` columns are ``auto_now_add``
they are back-dated with ``.update(created_at=...)`` after creation.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.tenant_sentiment import (
    MAX_SNIPPET_CHARS,
    _clean_sentiment_payload,
    build_tenant_sentiment,
    gather_consumer_feedback,
    get_or_refresh_tenant_sentiment,
)


def _mid_year(year: int):
    """Midday on July 2 of ``year`` (comfortably inside the calendar year)."""
    return timezone.now().replace(
        year=year, month=7, day=2, hour=12, minute=0, second=0, microsecond=0
    )


# A well-formed AI payload the mocked generate_json returns for happy paths.
# Both quotes are substrings of snippets the fixtures feed in.
_GOOD_AI_PAYLOAD = {
    "overall_sentiment": "positive",
    "positive_pct": 80,
    "summary": "Consumers loved the flavor and the booth experience.",
    "themes": [
        {"label": "Loved the flavor", "tone": "positive"},
        {"label": "Too sweet for some", "tone": "negative"},
    ],
    "quotes": [
        {"text": "This is the best drink I have ever tasted", "tone": "positive"},
    ],
}


@pytest.mark.django_db(transaction=True)
class TestGatherConsumerFeedback(AmbassadorsGraphQLTestCase):
    """gather_consumer_feedback: sources, ordering, dedup, year, bounding."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.other_tenant = self.create_tenant(name="Other Co")
        self.event = self.create_event(name="Demo @ Store", tenant=self.tenant)

    # -- legacy ConsumerFeedback helpers --------------------------------

    def _legacy_feedback(
        self,
        *,
        quotes=None,
        positive_stories=None,
        feedback=None,
        reasons_to_decline=None,
        demographics=None,
        when=None,
        event=None,
    ) -> recap_models.ConsumerFeedback:
        """Create a legacy recap + one ConsumerFeedback row, optionally dated."""
        recap = recap_models.Recap.objects.create(
            name="legacy recap",
            event=event or self.event,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        cf = recap_models.ConsumerFeedback.objects.create(
            recap=recap,
            quotes=quotes,
            positive_stories=positive_stories,
            feedback=feedback,
            reasons_to_decline=reasons_to_decline,
            demographics=demographics,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        if when is not None:
            recap_models.ConsumerFeedback.objects.filter(id=cf.id).update(
                created_at=when
            )
        return cf

    # -- custom CustomFieldValue helpers --------------------------------

    def _custom_field_value(
        self, field_name: str, value: str, *, when=None, tenant=None, event=None
    ) -> recap_models.CustomFieldValue:
        """Create the minimal custom-recap tree + one CustomFieldValue.

        Builds a field type / template / section / field / recap / value chain
        (mirrors test_repair_girl_beer_template.py) so the value row is
        reachable by the tenant→custom recap traversal and the field-name regex.
        """
        tenant = tenant or self.tenant
        event = event or self.event
        ft, _ = recap_models.CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.system_user}
        )
        event_type = self.create_event_type(
            name=f"ET {field_name[:10]}", tenant=tenant
        )
        template = recap_models.CustomRecapTemplate.objects.create(
            name=f"tmpl {field_name[:10]}",
            event_type=event_type,
            tenant=tenant,
            created_by=self.system_user,
        )
        section = recap_models.RecapSection.objects.create(
            tenant=tenant, name="Consumer Interaction", created_by=self.system_user
        )
        field = recap_models.CustomField.objects.create(
            custom_recap_template=template,
            recap_section=section,
            name=field_name,
            custom_field_type=ft,
            created_by=self.system_user,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="custom recap",
            event=event,
            tenant=tenant,
            custom_recap_template=template,
            created_by=self.system_user,
        )
        cfv = recap_models.CustomFieldValue.objects.create(
            custom_recap=recap,
            custom_field=field,
            value=value,
            created_by=self.system_user,
        )
        if when is not None:
            recap_models.CustomFieldValue.objects.filter(id=cfv.id).update(
                created_at=when
            )
        return cfv

    # -- tests ----------------------------------------------------------

    def test_empty_tenant_returns_empty_list(self):
        assert gather_consumer_feedback(self.tenant.id) == []

    def test_collects_legacy_feedback_columns(self):
        self._legacy_feedback(
            quotes="Great taste",
            positive_stories="A regular came back twice",
            feedback="Wanted a bigger sample",
            reasons_to_decline="Already a loyal customer",
            demographics="mostly 25-34 women",  # must be EXCLUDED
        )
        snippets = gather_consumer_feedback(self.tenant.id)
        assert "Great taste" in snippets
        assert "A regular came back twice" in snippets
        assert "Wanted a bigger sample" in snippets
        assert "Already a loyal customer" in snippets
        # demographics is not consumer feedback -> never gathered.
        assert "mostly 25-34 women" not in snippets

    def test_collects_custom_feedback_by_field_name(self):
        # A feedback-named field is gathered; a non-feedback field is not.
        self._custom_field_value("Consumer quotes", "Loved the cans")
        self._custom_field_value("Foot traffic per hour", "120")
        snippets = gather_consumer_feedback(self.tenant.id)
        assert "Loved the cans" in snippets
        assert "120" not in snippets

    def test_tenant_isolation(self):
        self._legacy_feedback(quotes="ours")
        other_event = self.create_event(name="theirs", tenant=self.other_tenant)
        self._legacy_feedback(quotes="theirs", event=other_event)
        snippets = gather_consumer_feedback(self.tenant.id)
        assert "ours" in snippets
        assert "theirs" not in snippets

    def test_dedup_case_insensitive(self):
        self._legacy_feedback(quotes="Loved it", positive_stories="loved it")
        self._legacy_feedback(feedback="LOVED IT")
        snippets = gather_consumer_feedback(self.tenant.id)
        # All three normalise to the same text -> exactly one survives.
        assert sum(1 for s in snippets if s.lower() == "loved it") == 1

    def test_year_filter(self):
        self._legacy_feedback(quotes="from 2024", when=_mid_year(2024))
        self._legacy_feedback(quotes="from 2025", when=_mid_year(2025))

        all_time = gather_consumer_feedback(self.tenant.id)
        assert "from 2024" in all_time and "from 2025" in all_time

        only_2024 = gather_consumer_feedback(self.tenant.id, year=2024)
        assert "from 2024" in only_2024
        assert "from 2025" not in only_2024

    def test_max_snippets_bound(self):
        for i in range(10):
            self._legacy_feedback(quotes=f"snippet number {i}")
        snippets = gather_consumer_feedback(self.tenant.id, max_snippets=4)
        assert len(snippets) == 4

    def test_max_chars_bound(self):
        # Each snippet ~50 chars; a 120-char budget admits 2 then stops.
        for i in range(10):
            self._legacy_feedback(quotes=f"feedback snippet {i} " + ("x" * 30))
        snippets = gather_consumer_feedback(
            self.tenant.id, max_snippets=100, max_chars=120
        )
        total = sum(len(s) for s in snippets)
        assert total <= 120
        # The budget must have actually clipped the set (not all 10 returned).
        assert len(snippets) < 10

    def test_long_snippet_is_trimmed(self):
        self._legacy_feedback(quotes="y" * 1000)
        snippets = gather_consumer_feedback(self.tenant.id)
        assert len(snippets) == 1
        assert len(snippets[0]) <= MAX_SNIPPET_CHARS

    def test_zero_bounds_return_empty(self):
        self._legacy_feedback(quotes="something")
        assert gather_consumer_feedback(self.tenant.id, max_snippets=0) == []
        assert gather_consumer_feedback(self.tenant.id, max_chars=0) == []


class TestCleanSentimentPayload:
    """The defensive cleaner: clamp/drop malformed AI output (pure, no DB)."""

    SNIPPETS = [
        "This is the best drink I have ever tasted",
        "Too sweet for my taste but my friend loved it",
        "The booth staff were super friendly",
    ]

    def test_none_on_non_dict(self):
        assert _clean_sentiment_payload(None, self.SNIPPETS) is None
        assert _clean_sentiment_payload("nope", self.SNIPPETS) is None

    def test_none_when_summary_missing_or_blank(self):
        assert _clean_sentiment_payload({"summary": ""}, self.SNIPPETS) is None
        assert _clean_sentiment_payload({"summary": "   "}, self.SNIPPETS) is None
        assert _clean_sentiment_payload({}, self.SNIPPETS) is None

    def test_bad_enum_and_pct_clamped(self):
        out = _clean_sentiment_payload(
            {
                "overall_sentiment": "ecstatic",  # not a valid enum
                "positive_pct": 250,  # over 100
                "summary": "Good overall.",
                "themes": [],
                "quotes": [],
            },
            self.SNIPPETS,
        )
        assert out["overall_sentiment"] == "mixed"  # fell back
        assert out["positive_pct"] == 100  # clamped

    def test_negative_pct_and_garbage_pct_clamped(self):
        assert (
            _clean_sentiment_payload(
                {"summary": "x", "positive_pct": -5}, self.SNIPPETS
            )["positive_pct"]
            == 0
        )
        assert (
            _clean_sentiment_payload(
                {"summary": "x", "positive_pct": "lots"}, self.SNIPPETS
            )["positive_pct"]
            == 0
        )

    def test_malformed_themes_dropped_and_tone_guarded(self):
        out = _clean_sentiment_payload(
            {
                "summary": "Mixed bag.",
                "themes": [
                    {"label": "Good flavor", "tone": "positive"},
                    {"label": "", "tone": "positive"},  # blank label -> dropped
                    {"tone": "negative"},  # no label -> dropped
                    "not a dict",  # -> dropped
                    {"label": "Weird tone", "tone": "spicy"},  # tone -> neutral
                ],
            },
            self.SNIPPETS,
        )
        labels = [t["label"] for t in out["themes"]]
        assert labels == ["Good flavor", "Weird tone"]
        assert out["themes"][1]["tone"] == "neutral"  # guarded

    def test_themes_capped_at_five(self):
        out = _clean_sentiment_payload(
            {
                "summary": "Many themes.",
                "themes": [
                    {"label": f"theme {i}", "tone": "neutral"} for i in range(9)
                ],
            },
            self.SNIPPETS,
        )
        assert len(out["themes"]) == 5

    def test_fabricated_quotes_dropped_verbatim_kept(self):
        out = _clean_sentiment_payload(
            {
                "summary": "Quotes test.",
                "quotes": [
                    # Verbatim substring of a snippet -> KEPT.
                    {"text": "the best drink I have ever tasted", "tone": "positive"},
                    # Fabricated / not in any snippet -> DROPPED.
                    {"text": "I will tell all my friends about this", "tone": "positive"},
                    {"text": "", "tone": "neutral"},  # blank -> dropped
                    "nope",  # non-dict -> dropped
                ],
            },
            self.SNIPPETS,
        )
        texts = [q["text"] for q in out["quotes"]]
        assert texts == ["the best drink I have ever tasted"]

    def test_quotes_capped_at_three(self):
        # Four snippets, all verbatim-returned; cleaner caps the kept list at 3.
        snippets = ["alpha quote", "beta quote", "gamma quote", "delta quote"]
        out = _clean_sentiment_payload(
            {
                "summary": "Lots of quotes.",
                "quotes": [{"text": s, "tone": "positive"} for s in snippets],
            },
            snippets,
        )
        assert len(out["quotes"]) == 3

    def test_no_quotes_kept_when_no_snippets(self):
        out = _clean_sentiment_payload(
            {"summary": "x", "quotes": [{"text": "anything", "tone": "positive"}]},
            [],
        )
        assert out["quotes"] == []


@pytest.mark.django_db(transaction=True)
class TestBuildTenantSentiment(AmbassadorsGraphQLTestCase):
    """build_tenant_sentiment: thin-data + AI-failure degrade; mocked happy path."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.event = self.create_event(name="Demo", tenant=self.tenant)

    def _feedback(self, quotes):
        recap = recap_models.Recap.objects.create(
            name="r", event=self.event,
            created_by=self.system_user, updated_by=self.system_user,
        )
        recap_models.ConsumerFeedback.objects.create(
            recap=recap, quotes=quotes,
            created_by=self.system_user, updated_by=self.system_user,
        )

    def _seed_enough(self):
        """Three+ distinct snippets, including the text the good payload quotes."""
        self._feedback("This is the best drink I have ever tasted")
        self._feedback("Too sweet for my taste but my friend loved it")
        self._feedback("The booth staff were super friendly")

    def test_too_little_data_returns_none_without_calling_ai(self):
        # Only two snippets (< MIN_SNIPPETS_FOR_SUMMARY) -> None, AI NOT called.
        self._feedback("only one")
        self._feedback("only two")
        with patch("utils.ai_text.generate_json") as mock_ai:
            result = build_tenant_sentiment(self.tenant.id)
        assert result is None
        mock_ai.assert_not_called()

    def test_ai_failure_returns_none(self):
        self._seed_enough()
        # generate_json returns None on any mid-flight failure.
        with patch("utils.ai_text.generate_json", return_value=None) as mock_ai:
            result = build_tenant_sentiment(self.tenant.id)
        assert result is None
        mock_ai.assert_called_once()

    def test_ai_unavailable_exception_returns_none(self):
        self._seed_enough()
        from utils.ai_text import AiUnavailable

        with patch(
            "utils.ai_text.generate_json", side_effect=AiUnavailable("no key")
        ):
            result = build_tenant_sentiment(self.tenant.id)
        assert result is None

    def test_happy_path_cleans_and_returns_payload(self):
        self._seed_enough()
        with patch(
            "utils.ai_text.generate_json", return_value=dict(_GOOD_AI_PAYLOAD)
        ) as mock_ai:
            result = build_tenant_sentiment(self.tenant.id)

        mock_ai.assert_called_once()
        # The schema is passed through to generate_json as a kwarg.
        assert "schema" in mock_ai.call_args.kwargs
        assert result is not None
        assert result["overall_sentiment"] == "positive"
        assert result["positive_pct"] == 80
        assert result["summary"].startswith("Consumers loved")
        assert len(result["themes"]) == 2
        # The single quote is verbatim from a seeded snippet -> kept.
        assert len(result["quotes"]) == 1
        assert result["quotes"][0]["text"].startswith("This is the best drink")

    def test_happy_path_drops_fabricated_quote(self):
        self._seed_enough()
        payload = dict(_GOOD_AI_PAYLOAD)
        payload["quotes"] = [
            {"text": "This is the best drink I have ever tasted", "tone": "positive"},
            {"text": "Totally made up sentence not in any snippet", "tone": "positive"},
        ]
        with patch("utils.ai_text.generate_json", return_value=payload):
            result = build_tenant_sentiment(self.tenant.id)
        assert result is not None
        texts = [q["text"] for q in result["quotes"]]
        assert "Totally made up sentence not in any snippet" not in texts
        assert any(t.startswith("This is the best drink") for t in texts)


@pytest.mark.django_db(transaction=True)
class TestGetOrRefreshTenantSentiment(AmbassadorsGraphQLTestCase):
    """The snapshot front door: serve-fresh / regenerate+persist / last-good."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Girl Beer")
        self.event = self.create_event(name="Demo", tenant=self.tenant)

    def _feedback(self, quotes, when=None):
        recap = recap_models.Recap.objects.create(
            name="r", event=self.event,
            created_by=self.system_user, updated_by=self.system_user,
        )
        cf = recap_models.ConsumerFeedback.objects.create(
            recap=recap, quotes=quotes,
            created_by=self.system_user, updated_by=self.system_user,
        )
        if when is not None:
            recap_models.ConsumerFeedback.objects.filter(id=cf.id).update(
                created_at=when
            )

    def _seed_enough(self, when=None):
        self._feedback("This is the best drink I have ever tasted", when=when)
        self._feedback("Too sweet for my taste but my friend loved it", when=when)
        self._feedback("The booth staff were super friendly", when=when)

    def test_regenerates_and_persists_then_serves_cache(self):
        from tenants.models import TenantSentimentSnapshot

        self._seed_enough()
        with patch(
            "utils.ai_text.generate_json", return_value=dict(_GOOD_AI_PAYLOAD)
        ) as mock_ai:
            payload, generated_at = get_or_refresh_tenant_sentiment(self.tenant.id)

        assert payload is not None
        assert generated_at is not None
        mock_ai.assert_called_once()  # regenerated on the cold cache
        snap = TenantSentimentSnapshot.objects.get(tenant=self.tenant, year=None)
        assert snap.sample_size == 3
        assert snap.payload["overall_sentiment"] == "positive"

        # Second call within the freshness window serves the cache (no AI call).
        with patch("utils.ai_text.generate_json") as mock_ai_2:
            payload2, _ = get_or_refresh_tenant_sentiment(self.tenant.id)
        mock_ai_2.assert_not_called()
        assert payload2 == payload
        assert TenantSentimentSnapshot.objects.filter(tenant=self.tenant).count() == 1

    def test_too_little_data_returns_none_none(self):
        self._feedback("only one")  # < 3 snippets
        with patch("utils.ai_text.generate_json") as mock_ai:
            payload, generated_at = get_or_refresh_tenant_sentiment(self.tenant.id)
        assert payload is None and generated_at is None
        mock_ai.assert_not_called()

    def test_falls_back_to_last_good_when_regeneration_fails(self):
        from tenants.models import TenantSentimentSnapshot

        self._seed_enough()
        # Seed a stale-but-good snapshot, then force its age past the window.
        good = TenantSentimentSnapshot.objects.create(
            tenant=self.tenant, year=None, sample_size=3,
            payload={
                "overall_sentiment": "positive", "positive_pct": 75,
                "summary": "Earlier good read.", "themes": [], "quotes": [],
            },
        )
        old = timezone.now() - timezone.timedelta(hours=48)
        TenantSentimentSnapshot.objects.filter(id=good.id).update(generated_at=old)

        # Stale -> tries to regenerate, but AI fails -> serve the last good one.
        with patch("utils.ai_text.generate_json", return_value=None):
            payload, generated_at = get_or_refresh_tenant_sentiment(
                self.tenant.id, max_age_hours=24
            )
        assert payload is not None
        assert payload["summary"] == "Earlier good read."
        # No NEW snapshot was written (regeneration failed).
        assert TenantSentimentSnapshot.objects.filter(tenant=self.tenant).count() == 1

    def test_year_partitions_cache(self):
        from tenants.models import TenantSentimentSnapshot

        # Seed enough feedback dated into 2025 so BOTH the all-time and the
        # year=2025 gathers find >= 3 snippets.
        self._seed_enough(when=_mid_year(2025))
        with patch(
            "utils.ai_text.generate_json", return_value=dict(_GOOD_AI_PAYLOAD)
        ):
            get_or_refresh_tenant_sentiment(self.tenant.id, year=None)
            get_or_refresh_tenant_sentiment(self.tenant.id, year=2025)
        # All-time and per-year snapshots coexist as separate rows.
        assert TenantSentimentSnapshot.objects.filter(
            tenant=self.tenant, year=None
        ).count() == 1
        assert TenantSentimentSnapshot.objects.filter(
            tenant=self.tenant, year=2025
        ).count() == 1
