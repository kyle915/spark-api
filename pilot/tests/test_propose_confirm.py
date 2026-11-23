"""Propose-then-confirm helpers: column guesses and ambiguous stores.

Model suggestions are returned for a person to turn on. They are not written
into the resolved map, and an ambiguous store match does not pick a retailer.
"""

import pytest
from django.core.cache import cache

from events.tests.base import EventsGraphQLTestCase
from pilot.column_guesses import FIELD_LABELS, describe_column_map, lock_column_map
from pilot.imports import (
    _HEADER_SYNONYMS,
    _map_for_confirm,
    _row_fingerprint,
    resolve_canonical_store,
)
from pilot.store_match_why import plain_store_match_reason


def test_rule_match_is_applied_and_model_guess_is_not(settings, monkeypatch):
    settings.OPENAI_API_KEY = "sk-test"
    cache.clear()

    def fake(*_a, **_k):
        return {
            "columns": [
                {
                    "header": "Mystery Col",
                    "field": "notes",
                    "reason": "Header wording looks like notes",
                }
            ]
        }

    monkeypatch.setattr("utils.ai_text.generate_json", fake)
    resolved = {"visit_date": "Date"}
    payload = describe_column_map(
        ["Date", "Mystery Col"],
        resolved,
        synonyms=_HEADER_SYNONYMS,
        presets={},
        preset_key=None,
        explicit_map=None,
        include_model=True,
    )
    by_header = {row["header"]: row for row in payload["guesses"]}
    assert by_header["Date"]["applied"] is True
    assert by_header["Date"]["field"] == "visit_date"
    assert by_header["Date"]["source"] == "rules"
    assert "date" in by_header["Date"]["reason"].lower()
    assert by_header["Mystery Col"]["applied"] is False
    assert by_header["Mystery Col"]["source"] == "model"
    assert by_header["Mystery Col"]["field"] == "notes"
    assert "notes" not in resolved


def test_confirm_without_a_map_is_an_error_not_inference():
    with pytest.raises(ValueError, match="columnMapJson"):
        _map_for_confirm(True, None)
    assert _map_for_confirm(True, {}) == {}
    assert _map_for_confirm(False, None) is None


def test_store_match_metadata_does_not_change_the_fingerprint():
    base = {"visit_date": "2026-01-01", "retailer_name": "FreshMart", "samples_distributed": 4}
    with_advice = {
        **base,
        "store_match": {
            "needs_confirm": True,
            "ai_reason": "wording that can change",
            "candidates": [{"id": 1, "name": "FreshMart"}],
        },
    }
    assert _row_fingerprint(base) == _row_fingerprint(with_advice)


def test_locked_map_does_not_fill_an_unchecked_synonym_or_model_guess():
    locked = lock_column_map(
        ["Date", "Store Name", "Mystery Col"],
        {"visit_date": "Date"},
        set(FIELD_LABELS),
    )
    assert locked == {"visit_date": "Date"}
    payload = describe_column_map(
        ["Date", "Store Name", "Mystery Col"],
        locked,
        synonyms=_HEADER_SYNONYMS,
        presets={},
        preset_key=None,
        explicit_map={"visit_date": "Date"},
        include_model=False,
        confirmed=True,
    )
    applied = {row["field"] for row in payload["guesses"] if row["applied"]}
    assert applied == {"visit_date"}
    mystery = next(row for row in payload["guesses"] if row["header"] == "Mystery Col")
    assert mystery["applied"] is False
    assert mystery["source"] == "none"


def test_store_reason_that_picks_one_candidate_is_dropped(settings, monkeypatch):
    settings.OPENAI_API_KEY = "sk-test"
    cache.clear()

    def fake(*_a, **_k):
        return {"why": "Use FreshMart Austin and ignore the other store."}

    monkeypatch.setattr("utils.ai_text.generate_json", fake)
    text = plain_store_match_reason(
        "FreshMart",
        [
            {"id": 1, "name": "FreshMart Austin", "city": "Austin", "state": "TX"},
            {"id": 2, "name": "FreshMart Dallas", "city": "Dallas", "state": "TX"},
        ],
    )
    assert text == ""


def test_store_reason_that_names_every_candidate_can_stay(settings, monkeypatch):
    settings.OPENAI_API_KEY = "sk-test"
    cache.clear()

    def fake(*_a, **_k):
        return {
            "why": "FreshMart Austin and FreshMart Dallas both match, so the row is still open."
        }

    monkeypatch.setattr("utils.ai_text.generate_json", fake)
    text = plain_store_match_reason(
        "FreshMart",
        [
            {"id": 1, "name": "FreshMart Austin", "city": "Austin", "state": "TX"},
            {"id": 2, "name": "FreshMart Dallas", "city": "Dallas", "state": "TX"},
        ],
    )
    assert "Austin" in text and "Dallas" in text


@pytest.mark.django_db(transaction=True)
class TestAmbiguousStoreMatch(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events import models as event_models

        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Ambiguous Store Brand")
        state = event_models.State.objects.create(
            name="Texas", code="TX", created_by=self.sys
        )
        austin = event_models.Location.objects.create(
            name="Austin", code="AUS", zip="78701", state=state, created_by=self.sys
        )
        dallas = event_models.Location.objects.create(
            name="Dallas", code="DAL", zip="75201", state=state, created_by=self.sys
        )
        self.create_retailer("FreshMart", "1 Congress Ave", "Pat", austin, self.tenant)
        self.create_retailer("FreshMart", "2 Main St", "Pat", dallas, self.tenant)

    def test_two_stores_are_listed_and_neither_is_chosen(self):
        info, code, message, candidates = resolve_canonical_store(
            tenant=self.tenant,
            retailer_name="FreshMart",
            store_number="",
            external_store_id="",
            address="",
            city="",
            state_code="",
            aliases={},
        )
        assert info is None
        assert code == "uncertain_store_match"
        assert "Nothing is matched until you choose" in message
        assert len(candidates) == 2
        names = {item["name"] for item in candidates}
        assert names == {"FreshMart"}
        cities = {item["city"] for item in candidates}
        assert cities == {"Austin", "Dallas"}
        assert all("nothing is matched" in item["reason"].lower() for item in candidates)
