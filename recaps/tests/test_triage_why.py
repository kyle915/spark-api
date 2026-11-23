"""Needs Eyes blurbs: rules stay, prose only when the label is Needs Eyes."""

from django.core.cache import cache

from recaps.review_triage import LABEL_CLEAN, LABEL_NEEDS_EYES
from recaps.triage_why import attach_triage_why, rules_why


def _row(label, reasons):
    return {"label": label, "reasons": reasons}


def test_clean_rows_get_no_sentence_and_no_model(settings, monkeypatch):
    settings.OPENAI_API_KEY = "sk-test"
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("model must not run for Clean")

    monkeypatch.setattr("utils.ai_text.generate_json", boom)
    cache.clear()
    by_id = {
        1: _row(LABEL_CLEAN, []),
        2: _row(
            LABEL_CLEAN,
            [{"code": "no_photos", "label": "There are no photos"}],
        ),
    }
    attach_triage_why(by_id)
    assert by_id[1]["why"] is None
    assert by_id[2]["why"] is None
    assert called["n"] == 0


def test_one_reason_is_a_fixed_sentence(settings, monkeypatch):
    settings.OPENAI_API_KEY = "sk-test"

    def boom(*_a, **_k):
        raise AssertionError("one reason does not need a model")

    monkeypatch.setattr("utils.ai_text.generate_json", boom)
    cache.clear()
    by_id = {
        7: _row(
            LABEL_NEEDS_EYES,
            [{"code": "shelf_oos_gap", "label": "Shelf photo shows an out-of-stock gap"}],
        )
    }
    attach_triage_why(by_id)
    assert by_id[7]["why"] == "Shelf photo shows an out-of-stock gap."
    assert by_id[7]["why_source"] == "rules"


def test_several_reasons_keep_model_prose_when_it_adds_no_numbers(settings, monkeypatch):
    settings.OPENAI_API_KEY = "sk-test"
    cache.clear()

    def fake(*_a, **_k):
        return {"why": "The shelf has a gap and a photo could not be read."}

    monkeypatch.setattr("utils.ai_text.generate_json", fake)
    reasons = [
        {"code": "shelf_oos_gap", "label": "Shelf photo shows an out-of-stock gap"},
        {"code": "photo_unreadable", "label": "A filed photo could not be read"},
    ]
    by_id = {3: _row(LABEL_NEEDS_EYES, reasons)}
    attach_triage_why(by_id)
    assert by_id[3]["why_source"] == "model"
    assert "gap" in by_id[3]["why"].lower()
    assert by_id[3]["label"] == LABEL_NEEDS_EYES


def test_model_line_with_a_new_number_is_dropped(settings, monkeypatch):
    settings.OPENAI_API_KEY = "sk-test"
    cache.clear()

    def fake(*_a, **_k):
        return {"why": "About 400 people were sampled and the shelf has a gap."}

    monkeypatch.setattr("utils.ai_text.generate_json", fake)
    reasons = [
        {"code": "shelf_oos_gap", "label": "Shelf photo shows an out-of-stock gap"},
        {"code": "photo_unreadable", "label": "A filed photo could not be read"},
    ]
    by_id = {4: _row(LABEL_NEEDS_EYES, reasons)}
    attach_triage_why(by_id)
    assert by_id[4]["why_source"] == "rules"
    assert "400" not in by_id[4]["why"]
    assert by_id[4]["why"] == rules_why(reasons)


def test_no_key_joins_reasons_without_calling_the_model(settings, monkeypatch):
    settings.OPENAI_API_KEY = ""
    cache.clear()

    def boom(*_a, **_k):
        raise AssertionError("no key means no call")

    monkeypatch.setattr("utils.ai_text.generate_json", boom)
    reasons = [
        {"code": "no_photos", "label": "There are no photos"},
        {"code": "no_feedback", "label": "No feedback was written"},
    ]
    by_id = {5: _row(LABEL_NEEDS_EYES, reasons)}
    attach_triage_why(by_id)
    assert by_id[5]["why_source"] == "rules"
    assert by_id[5]["why"] == "There are no photos, and no feedback was written."
