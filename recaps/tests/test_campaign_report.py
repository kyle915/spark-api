"""Coverage for the Client Campaign Report aggregation + share token.

The campaign report rolls up one Request's events → recaps into headline
KPIs, a photo gallery, a BA roster, and consumer quotes. It MUST handle
both recap shapes — legacy ``Recap`` (typed columns + ``consumer_engagements``
child) and custom-template ``CustomRecap`` (free-text ``CustomFieldValue``
rows) — and the two must roll into the same totals.

These tests exercise the framework-free aggregation helpers in
``recaps.report_service`` directly with lightweight stand-in objects (no
DB), in the same "pure helper" style as
``test_custom_recap_card_aggregates.py``. The signed-token tests use the
real :class:`django.core.signing.TimestampSigner`.
"""

from __future__ import annotations

import pytest

from recaps import report_service as rs
from recaps.report_tokens import (
    BadSignature,
    make_report_token,
    verify_report_token,
)


@pytest.fixture(autouse=True)
def _gcs_bucket(settings):
    # Photo URLs come from utils.gcs.public_url(), which returns None when
    # settings.GS_BUCKET_NAME is empty (no bucket configured in CI) — pin a
    # dummy bucket so the URL-shape assertions run everywhere.
    settings.GS_BUCKET_NAME = "spark-test-bucket"


# ---------------------------------------------------------------------------
# Lightweight stand-ins. Mimic just the attributes the helpers read so the
# aggregation logic can be pinned without a database / migrations.
# ---------------------------------------------------------------------------
class _Mgr:
    """Stands in for a Django reverse manager (only ``.all()`` is used)."""

    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _legacy_recap(*, event_name="Event", **kw):
    """A legacy Recap stand-in with sensible empty children."""
    defaults = dict(
        total_engagements=0,
        products_sold=0,
        total_cans_sold=0,
        total_packs_sold=0,
        external_ba_name=None,
        ambassador=None,
        event_id=1,
        consumer_engagements=_Mgr([]),
        product_samples=_Mgr([]),
        consumer_feedback=_Mgr([]),
        recap_files=_Mgr([]),
    )
    defaults.update(kw)
    obj = _Obj(**defaults)
    obj.event = _Obj(name=event_name)
    return obj


def _custom_recap(field_pairs, *, event_name="Event", total_engagements=0, **kw):
    """A CustomRecap stand-in whose KPIs live in CustomFieldValue rows."""
    cfvs = [
        _Obj(custom_field=_Obj(name=name), value=value)
        for name, value in field_pairs
    ]
    defaults = dict(
        total_engagements=total_engagements,
        external_ba_name=None,
        ambassador=None,
        event_id=2,
        custom_field_value=_Mgr(cfvs),
        custom_recap_product_sample=_Mgr([]),
        custom_recap_files=_Mgr([]),
    )
    defaults.update(kw)
    obj = _Obj(**defaults)
    obj.event = _Obj(name=event_name)
    return obj


# ---------------------------------------------------------------------------
# Legacy recap KPI rollup.
# ---------------------------------------------------------------------------
def test_legacy_recap_accumulates_typed_columns_and_children():
    recap = _legacy_recap(
        total_engagements=200,
        products_sold=50,
        total_cans_sold=30,
        total_packs_sold=20,
        consumer_engagements=_Mgr(
            [
                _Obj(
                    total_consumer=100,
                    first_time_consumers=40,
                    brand_aware_consumers=55,
                    willing_to_purchase_consumers=70,
                )
            ]
        ),
        product_samples=_Mgr([_Obj(quantity=25), _Obj(quantity=5)]),
    )
    kpis = rs.CampaignReportKpis()
    rs._accumulate_legacy(recap, kpis)

    assert kpis.total_engagements == 200
    assert kpis.products_sold == 50
    assert kpis.cans_sold == 30
    assert kpis.packs_sold == 20
    assert kpis.consumers_reached == 100
    assert kpis.first_time_consumers == 40
    assert kpis.brand_aware_consumers == 55
    assert kpis.willing_to_purchase == 70
    assert kpis.samples_distributed == 30  # 25 + 5


def test_legacy_recap_tolerates_missing_engagements_row():
    recap = _legacy_recap(total_engagements=10)  # no consumer_engagements
    kpis = rs.CampaignReportKpis()
    rs._accumulate_legacy(recap, kpis)
    assert kpis.total_engagements == 10
    assert kpis.consumers_reached == 0


# ---------------------------------------------------------------------------
# Custom recap KPI rollup — free-text fields, label-matched + parsed.
# ---------------------------------------------------------------------------
def test_custom_recap_maps_free_text_fields_to_kpis():
    recap = _custom_recap(
        [
            ("Consumers Sampled", "70"),
            ("First Time Trying", "30"),
            ("Knew About Brand", "25"),
            ("Willing to Purchase", "40"),
            ("Would NOT be willing to purchase", "5"),
            ("Single Cans Sold", "12"),
            ("6-Packs Sold", "8"),
        ],
        total_engagements=12,
    )
    kpis = rs.CampaignReportKpis()
    rs._accumulate_custom(recap, kpis)

    assert kpis.total_engagements == 12
    assert kpis.consumers_reached == 70
    assert kpis.first_time_consumers == 30
    assert kpis.brand_aware_consumers == 25
    # "not willing" must be excluded from willing_to_purchase.
    assert kpis.willing_to_purchase == 40
    assert kpis.cans_sold == 12
    assert kpis.packs_sold == 8
    assert kpis.products_sold == 20  # cans + packs sold sum
    # No structured product samples → falls back to "consumers sampled".
    assert kpis.samples_distributed == 70


def test_custom_recap_prefers_structured_samples_over_consumers_sampled():
    recap = _custom_recap(
        [("Consumers Sampled", "70")],
        custom_recap_product_sample=_Mgr([_Obj(quantity=15), _Obj(quantity=10)]),
    )
    kpis = rs.CampaignReportKpis()
    rs._accumulate_custom(recap, kpis)
    assert kpis.samples_distributed == 25  # structured wins, not 70


def test_legacy_and_custom_recaps_sum_into_the_same_totals():
    """A request mixing both recap shapes adds them together."""
    legacy = _legacy_recap(
        products_sold=10,
        consumer_engagements=_Mgr([_Obj(total_consumer=100)]),
    )
    custom = _custom_recap(
        [("Consumers Sampled", "50"), ("Cans Sold", "10")]
    )
    kpis = rs.CampaignReportKpis()
    rs._accumulate_legacy(legacy, kpis)
    rs._accumulate_custom(custom, kpis)

    assert kpis.consumers_reached == 150  # 100 + 50
    assert kpis.products_sold == 20  # 10 legacy + 10 custom cans


# ---------------------------------------------------------------------------
# Photo gallery — image URLs only, capped, captioned with the event name.
# ---------------------------------------------------------------------------
def test_photos_keep_images_drop_non_images():
    recap = _legacy_recap(
        event_name="Event A",
        recap_files=_Mgr(
            [
                _Obj(file=_Obj(name="recap_files/photo.jpg")),
                _Obj(file=_Obj(name="recap_files/doc.pdf")),
                _Obj(file=_Obj(name="recap_files/img2.PNG")),
            ]
        ),
    )
    photos = rs._collect_photos([recap], [])
    assert len(photos) == 2
    assert all(p.caption == "Event A" for p in photos)
    assert all(p.url.endswith((".jpg", ".PNG")) for p in photos)


def test_photos_respect_the_cap(monkeypatch):
    monkeypatch.setattr(rs, "MAX_PHOTOS", 3)
    files = [_Obj(file=_Obj(name=f"recap_files/p{i}.jpg")) for i in range(10)]
    recap = _legacy_recap(recap_files=_Mgr(files))
    assert len(rs._collect_photos([recap], [])) == 3


# ---------------------------------------------------------------------------
# BA roster — real BA precedence, external fallback, dedup, event count.
# ---------------------------------------------------------------------------
def test_ba_roster_dedupes_and_counts_distinct_events():
    user = _Obj(first_name="Jane", last_name="Doe", email="jane@x.com")
    ambassador = _Obj(id=9, user=user)

    legacy = [
        _legacy_recap(ambassador=ambassador, event_id=1),
        _legacy_recap(ambassador=ambassador, event_id=2),
        _legacy_recap(ambassador=ambassador, event_id=1),  # same event again
    ]
    custom = [
        _custom_recap([], ambassador=None, external_ba_name="Bob Helper", event_id=3),
        _custom_recap([], ambassador=None, external_ba_name="bob helper", event_id=4),
    ]
    roster = rs._collect_ambassadors(legacy, custom)

    jane = next(b for b in roster if b.name == "Jane Doe")
    bob = next(b for b in roster if b.name == "Bob Helper")
    assert jane.event_count == 2 and jane.is_external is False
    assert bob.event_count == 2 and bob.is_external is True  # dedup by name


def test_ba_roster_skips_blank_external_names():
    recap = _legacy_recap(ambassador=None, external_ba_name="   ")
    assert rs._collect_ambassadors([recap], []) == []


# ---------------------------------------------------------------------------
# Highlights — quotes/positive stories, both shapes, deduped, attributed.
# ---------------------------------------------------------------------------
def test_highlights_from_legacy_feedback_with_source():
    recap = _legacy_recap(
        event_name="Costco Demo",
        consumer_feedback=_Mgr(
            [_Obj(quotes="Amazing event!", positive_stories="Came back twice.")]
        ),
    )
    highlights = rs._collect_highlights([recap], [])
    texts = [q.text for q in highlights]
    assert "Amazing event!" in texts
    assert "Came back twice." in texts
    assert all(q.source == "Costco Demo" for q in highlights)


def test_highlights_from_custom_quote_fields():
    recap = _custom_recap(
        [
            ("Best Consumer Quote", "I love this drink!"),
            ("Cans Sold", "10"),  # not a quote field — ignored
        ]
    )
    highlights = rs._collect_highlights([], [recap])
    assert [q.text for q in highlights] == ["I love this drink!"]


def test_highlights_dedupe_identical_text():
    r1 = _legacy_recap(consumer_feedback=_Mgr([_Obj(quotes="Great!")]))
    r2 = _legacy_recap(consumer_feedback=_Mgr([_Obj(quotes="great!")]))
    highlights = rs._collect_highlights([r1, r2], [])
    assert len(highlights) == 1


def test_highlights_trim_overlong_quotes(monkeypatch):
    monkeypatch.setattr(rs, "MAX_QUOTE_CHARS", 10)
    recap = _legacy_recap(consumer_feedback=_Mgr([_Obj(quotes="x" * 50)]))
    text = rs._collect_highlights([recap], [])[0].text
    assert len(text) == 10 and text.endswith("…")


# ---------------------------------------------------------------------------
# Date range formatting.
# ---------------------------------------------------------------------------
def test_date_range_none_when_no_event_dates():
    events = [_Obj(date=None), _Obj(date=None)]
    assert rs._format_date_range(events) is None


def test_date_range_same_day():
    from datetime import datetime

    d = datetime(2026, 4, 1, 10, 0)
    assert rs._format_date_range([_Obj(date=d)]) == "Apr 1, 2026"


def test_date_range_within_year():
    from datetime import datetime

    events = [
        _Obj(date=datetime(2026, 4, 1)),
        _Obj(date=datetime(2026, 4, 30)),
    ]
    assert rs._format_date_range(events) == "Apr 1 – Apr 30, 2026"


# ---------------------------------------------------------------------------
# JSON serialization — camelCase keys, no shareToken, nested blocks.
# ---------------------------------------------------------------------------
def test_report_to_dict_shape_and_omits_share_token():
    data = rs.CampaignReportData(
        request_id=42,
        brand_name="Liquid Death",
        title="Spring Sampling",
        date_range="Apr 1 – Apr 30, 2026",
        generated_at="2026-05-31T00:00:00+00:00",
        kpis=rs.CampaignReportKpis(events=3, recaps=5, consumers_reached=250),
        events=[
            rs.CampaignReportEventRow(
                id="7",
                name="Costco",
                date="2026-04-01T00:00:00",
                location_name="Austin",
                city="Austin",
                state="TX",
                coordinates="30.2,-97.7",
                recap_count=2,
            )
        ],
        photos=[rs.CampaignReportPhoto(url="https://x/y.jpg", caption="Costco")],
        ambassadors=[
            rs.CampaignReportBa(name="Jane Doe", is_external=False, event_count=2)
        ],
        highlights=[rs.CampaignReportQuote(text="Loved it", source="Costco")],
    )
    out = rs.report_to_dict(data)

    assert "shareToken" not in out
    assert out["requestId"] == "42"
    assert out["brandName"] == "Liquid Death"
    assert out["kpis"]["consumersReached"] == 250
    assert out["events"][0]["locationName"] == "Austin"
    assert out["events"][0]["recapCount"] == 2
    assert out["photos"][0]["url"] == "https://x/y.jpg"
    assert out["ambassadors"][0]["isExternal"] is False
    assert out["ambassadors"][0]["eventCount"] == 2
    assert out["highlights"][0]["text"] == "Loved it"


# ---------------------------------------------------------------------------
# Share token round-trip + tamper / expiry semantics.
# ---------------------------------------------------------------------------
def test_share_token_round_trip():
    token = make_report_token(4242)
    assert verify_report_token(token) == 4242


def test_share_token_accepts_max_age_none():
    token = make_report_token(7)
    assert verify_report_token(token, max_age=None) == 7


def test_share_token_rejects_tampered_token():
    token = make_report_token(1)
    with pytest.raises(BadSignature):
        verify_report_token(token + "tamper")
