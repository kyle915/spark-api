"""Buyer pitch deck sell-story captions — rules + grounded model polish."""

from __future__ import annotations

from unittest.mock import patch

from brand_insights.deck_captions import (
    enrich_deck_photos,
    overlay_snapshot_captions,
    rules_photo_caption,
    rules_sell_story,
)


def test_rules_caption_prefers_pin_note():
    assert "Hand-picked" in rules_photo_caption(
        {"pin_note": "Hand-picked endcap shot", "store": "Sprouts"}
    )


def test_rules_caption_shelf_on_shelf_with_facings():
    text = rules_photo_caption(
        {
            "store": "Sprouts #12",
            "shelf": {"product_on_shelf": "yes", "facings": 4, "price": "3.99"},
        }
    )
    assert "Sprouts #12" in text
    assert "4 facing" in text
    assert "$3.99" in text


def test_rules_caption_uses_mention_theme_when_no_score():
    text = rules_photo_caption(
        {
            "store": "Whole Foods",
            "mention_themes": ["refreshing taste"],
            "score_caption": "",
            "shelf": {},
        }
    )
    assert "Whole Foods" in text
    assert "refreshing taste" in text


def test_rules_sell_story_grounded_in_kpis_only():
    text = rules_sell_story(
        brand_name="Torch",
        kpis={
            "totalVisits": 12,
            "storesActivated": 8,
            "totalSamplesDistributed": 400,
            "samplingLiftPct": 18.5,
        },
        mention_themes=["energy"],
        period_start="2026-11-01",
        period_end="2026-11-22",
    )
    assert "Torch" in text
    assert "12 visit" in text
    assert "8 store" in text
    assert "400 sample" in text
    assert "18.5%" in text
    assert "energy" in text


def test_overlay_restores_snapshot_caption_unless_live_pin():
    reminted = [
        {"id": "1", "url": "https://fresh/a.jpg", "caption": "scorer", "pin_note": ""},
        {
            "id": "2",
            "url": "https://fresh/b.jpg",
            "caption": "staff pin",
            "pin_note": "staff pin",
        },
    ]
    snap = [
        {"id": "1", "caption": "Sell-story caption.", "captionSource": "model"},
        {"id": "2", "caption": "Old snapshot", "captionSource": "rules"},
    ]
    out = overlay_snapshot_captions(reminted, snap)
    assert out[0]["url"] == "https://fresh/a.jpg"
    assert out[0]["caption"] == "Sell-story caption."
    assert out[0]["captionSource"] == "model"
    assert out[1]["caption"] == "staff pin"
    assert "pin_note" not in out[0]


def test_enrich_uses_rules_when_openai_unavailable():
    photos = [
        {
            "id": "99",
            "url": "https://x/a.jpg",
            "category": "Sampling photos",
            "caption": "",
            "store": "Sprouts",
            "retailer": "Sprouts",
            "market": "",
            "state": "CO",
            "eventDate": "2026-11-10",
            "skus": ["5MG"],
            "pin_note": "",
        }
    ]
    with (
        patch(
            "brand_insights.deck_captions._photo_facts_for_ids",
            return_value={
                99: {
                    "file_id": 99,
                    "recap_id": 1,
                    "score_caption": "Sampling table with product cans.",
                    "kind": "sampling",
                    "score": 80,
                    "shelf": {},
                    "mention_themes": ["taste"],
                }
            },
        ),
        patch(
            "brand_insights.deck_captions._model_polish",
            return_value=None,
        ),
    ):
        enriched, sell, source = enrich_deck_photos(
            1,
            photos,
            brand_name="Torch",
            kpis={"totalVisits": 3, "storesActivated": 2, "totalSamplesDistributed": 50},
            period_start="2026-11-01",
            period_end="2026-11-22",
            intro_note="",
        )
    assert len(enriched) == 1
    assert enriched[0]["caption"]
    assert enriched[0]["captionSource"] == "rules"
    assert "Torch" in sell
    assert source == "rules"
    assert "pin_note" not in enriched[0]


def test_enrich_keeps_model_caption_when_numbers_grounded():
    photos = [
        {
            "id": "7",
            "url": "https://x/a.jpg",
            "category": "Shelf",
            "caption": "",
            "store": "Sprouts",
            "retailer": "Sprouts",
            "skus": [],
            "pin_note": "",
        }
    ]
    model_reply = {
        "captions": [{"id": "7", "caption": "On shelf at Sprouts with 3 facings"}],
        "sell_story": "Torch ran 2 visits across 1 store from 2026-11-01 to 2026-11-22.",
    }
    with (
        patch(
            "brand_insights.deck_captions._photo_facts_for_ids",
            return_value={
                7: {
                    "file_id": 7,
                    "recap_id": 1,
                    "score_caption": "",
                    "kind": "shelf",
                    "score": 70,
                    "shelf": {"product_on_shelf": "yes", "facings": 3},
                    "mention_themes": [],
                }
            },
        ),
        patch(
            "brand_insights.deck_captions._model_polish",
            return_value=model_reply,
        ),
        patch("brand_insights.deck_captions.cache") as cache_mock,
    ):
        cache_mock.get.return_value = None
        enriched, sell, source = enrich_deck_photos(
            1,
            photos,
            brand_name="Torch",
            kpis={"totalVisits": 2, "storesActivated": 1},
            period_start="2026-11-01",
            period_end="2026-11-22",
        )
    assert enriched[0]["captionSource"] == "model"
    assert "3" in enriched[0]["caption"]
    assert source == "model"
    assert "2 visit" in sell


def test_enrich_kpi_sell_story_when_no_photos():
    enriched, sell, source = enrich_deck_photos(
        1,
        [],
        brand_name="Torch",
        kpis={"totalVisits": 5, "storesActivated": 3, "totalSamplesDistributed": 100},
        period_start="2026-11-01",
        period_end="2026-11-22",
        intro_note="",
    )
    assert enriched == []
    assert source == "rules"
    assert "Torch" in sell
    assert "5 visit" in sell
    assert "3 store" in sell


def test_clean_sell_story_keeps_long_text_under_sell_limit():
    from brand_insights.deck_captions import MAX_SELL_STORY_CHARS, _clean_sell_story

    long = (
        "Torch ran twelve visits across eight stores from 2026-11-01 to 2026-11-22, "
        "handing out samples and matching POS lift in scope for the buyer leave-behind. "
        "Consumers talked about taste and energy throughout the window of the program."
    )
    cleaned = _clean_sell_story(long)
    assert cleaned
    assert len(cleaned) <= MAX_SELL_STORY_CHARS
    assert "…" not in cleaned or len(long) > MAX_SELL_STORY_CHARS
    # Must not be clipped at the 140-char caption limit.
    assert len(cleaned) > 140


def test_transport_photo_id_not_in_allowed_caption_numbers():
    from brand_insights.deck_captions import _allowed_numbers, _numbers_ok

    prompt = {
        "id": "99",
        "store": "Sprouts",
        "shelf": {"product_on_shelf": "yes", "facings": 3},
        "rules_caption": "On shelf at Sprouts, 3 facings.",
    }
    allowed = _allowed_numbers(prompt)
    assert "3" in allowed
    assert "99" not in allowed
    assert not _numbers_ok("On shelf at Sprouts with 99 facings.", allowed)
    assert _numbers_ok("On shelf at Sprouts with 3 facings.", allowed)


def test_enrich_drops_model_caption_with_invented_number():
    photos = [
        {
            "id": "7",
            "url": "https://x/a.jpg",
            "category": "Shelf",
            "caption": "",
            "store": "Sprouts",
            "retailer": "Sprouts",
            "skus": [],
            "pin_note": "",
        }
    ]
    model_reply = {
        "captions": [{"id": "7", "caption": "On shelf at Sprouts with 99 facings"}],
        "sell_story": "Torch grew sales 250% this month.",
    }
    with (
        patch(
            "brand_insights.deck_captions._photo_facts_for_ids",
            return_value={
                7: {
                    "file_id": 7,
                    "recap_id": 1,
                    "score_caption": "",
                    "kind": "shelf",
                    "score": 70,
                    "shelf": {"product_on_shelf": "yes", "facings": 3},
                    "mention_themes": [],
                }
            },
        ),
        patch(
            "brand_insights.deck_captions._model_polish",
            return_value=model_reply,
        ),
        patch("brand_insights.deck_captions.cache") as cache_mock,
    ):
        cache_mock.get.return_value = None
        enriched, sell, source = enrich_deck_photos(
            1,
            photos,
            brand_name="Torch",
            kpis={"totalVisits": 2, "storesActivated": 1},
            period_start="2026-11-01",
            period_end="2026-11-22",
        )
    # Invented 99 dropped → rules caption kept; invented sell % dropped.
    assert enriched[0]["captionSource"] == "rules"
    assert "3 facing" in enriched[0]["caption"]
    assert source == "rules"
    assert "250" not in sell
