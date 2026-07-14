"""
Tests for the Campaign Report PDF builder (recaps/pdf.py).

Coverage is intentionally focused on pure-Python helpers so the suite
doesn't depend on WeasyPrint's native libs being installed in the
test environment. The final `build_campaign_report_pdf` call is
exercised via a `weasyprint.HTML.write_pdf` mock that verifies we
hand WeasyPrint a single composite HTML document with cover + per-
recap sections in the order requested.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from recaps.pdf import (
    _aggregate_engagements,
    _aggregate_units_sold,
    _extract_body,
    _format_date_range,
    build_campaign_report_pdf,
)


# ─── _extract_body ───────────────────────────────────────────────


def test_extract_body_pulls_inner_html_only():
    full = """
<!doctype html>
<html><head><title>x</title></head>
<body class="recap">
  <h1>Recap A</h1>
  <p>Sample</p>
</body></html>
"""
    body = _extract_body(full)
    assert "<h1>Recap A</h1>" in body
    assert "<!doctype html>" not in body
    assert "<html>" not in body
    assert "<head>" not in body


def test_extract_body_handles_no_body_tag_gracefully():
    # If a future refactor of build_recap_pdf_html drops the <body>
    # wrapper we should over-include rather than silently lose data.
    raw = "<div><h2>orphan</h2></div>"
    body = _extract_body(raw)
    assert "orphan" in body


# ─── _aggregate_engagements ──────────────────────────────────────


class _RelatedList:
    """Mimics a Django RelatedManager — `.all()` returns the iterable
    `_related_items` reads from."""

    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


def _make_recap_with_engagements(**fields):
    """Synthetic recap whose `consumer_engagements.all()` yields one
    engagement row populated with the supplied stats."""
    eng = SimpleNamespace(**fields)
    return SimpleNamespace(consumer_engagements=_RelatedList([eng]))


def test_aggregate_engagements_sums_across_recaps():
    recaps = [
        _make_recap_with_engagements(
            total_consumer=100,
            first_time_consumers=40,
            brand_aware_consumers=60,
            willing_to_purchase_consumers=35,
        ),
        _make_recap_with_engagements(
            total_consumer=80,
            first_time_consumers=30,
            brand_aware_consumers=50,
            willing_to_purchase_consumers=20,
        ),
    ]

    totals = _aggregate_engagements(recaps)

    assert totals["total_consumer"] == 180
    assert totals["first_time_consumers"] == 70
    assert totals["brand_aware_consumers"] == 110
    assert totals["willing_to_purchase_consumers"] == 55


def test_aggregate_engagements_skips_missing_rows():
    recap_a = _make_recap_with_engagements(
        total_consumer=50,
        first_time_consumers=10,
        brand_aware_consumers=20,
        willing_to_purchase_consumers=5,
    )
    # Recap with no engagement rows at all — should contribute zero
    # and not crash.
    recap_b = SimpleNamespace(consumer_engagements=_RelatedList([]))

    totals = _aggregate_engagements([recap_a, recap_b])

    assert totals["total_consumer"] == 50
    assert totals["first_time_consumers"] == 10


def test_aggregate_engagements_ignores_non_numeric():
    # Real DB rows can have None for any field. Make sure that
    # doesn't kill the sum or coerce to a weird type.
    recap = _make_recap_with_engagements(
        total_consumer=12,
        first_time_consumers=None,
        brand_aware_consumers="not-a-number",
        willing_to_purchase_consumers=8,
    )

    totals = _aggregate_engagements([recap])

    assert totals["total_consumer"] == 12
    assert totals["first_time_consumers"] == 0
    assert totals["brand_aware_consumers"] == 0
    assert totals["willing_to_purchase_consumers"] == 8


# ─── _aggregate_units_sold ───────────────────────────────────────


def _make_custom_recap(field_pairs):
    """Synthetic custom recap whose `custom_field_value.all()` yields rows
    with `.custom_field.name` + `.value` — the shape the units matcher
    reads (Neutonic/Borjomi/Girl Beer store sold units as free-text
    custom fields)."""
    cfvs = [
        SimpleNamespace(custom_field=SimpleNamespace(name=name), value=value)
        for name, value in field_pairs
    ]
    return SimpleNamespace(custom_field_value=_RelatedList(cfvs))


def _make_legacy_recap(products_sold):
    """Legacy recap: no custom fields, a typed products_sold column."""
    return SimpleNamespace(products_sold=products_sold)


def test_aggregate_units_sold_legacy_sums_products_sold():
    recaps = [_make_legacy_recap(120), _make_legacy_recap(30)]
    assert _aggregate_units_sold(recaps) == 150


def test_aggregate_units_sold_legacy_handles_none():
    # products_sold can be NULL on a legacy recap → treated as 0.
    recaps = [_make_legacy_recap(None), _make_legacy_recap(45)]
    assert _aggregate_units_sold(recaps) == 45


def test_aggregate_units_sold_custom_reads_packs_field():
    # Matches the Neutonic recap: "How many packs did consumers purchase?"
    recap = _make_custom_recap(
        [
            ("How many consumers were sampled?", "370"),
            ("How many packs did consumers purchase?", "86"),
        ]
    )
    assert _aggregate_units_sold([recap]) == 86


def test_aggregate_units_sold_custom_without_sales_field_contributes_zero():
    # A template with no cans/packs/sold field → None → 0, not a crash.
    recap = _make_custom_recap([("General demographics", "families")])
    assert _aggregate_units_sold([recap]) == 0


def test_aggregate_units_sold_mixes_legacy_and_custom():
    recaps = [
        _make_legacy_recap(100),
        _make_custom_recap(
            [("How many packs did consumers purchase?", "86")]
        ),
    ]
    assert _aggregate_units_sold(recaps) == 186


# ─── _format_date_range ──────────────────────────────────────────


def _recap_with_event_date(date_value):
    return SimpleNamespace(event=SimpleNamespace(date=date_value))


def test_format_date_range_single_date_renders_one_label():
    r = _recap_with_event_date(datetime(2026, 5, 10))
    assert _format_date_range([r]) == "May 10, 2026"


def test_format_date_range_multi_date_renders_span():
    a = _recap_with_event_date(datetime(2026, 5, 1))
    b = _recap_with_event_date(datetime(2026, 5, 20))
    label = _format_date_range([a, b])
    assert label == "May 01, 2026 – May 20, 2026"


def test_format_date_range_handles_unordered_input():
    # The function should sort internally — the caller-supplied order
    # is the recap order (for the cover-page sequence), not the
    # date-range order.
    later = _recap_with_event_date(datetime(2026, 5, 20))
    earlier = _recap_with_event_date(datetime(2026, 5, 1))
    label = _format_date_range([later, earlier])
    assert label == "May 01, 2026 – May 20, 2026"


def test_format_date_range_falls_back_to_em_dash_when_empty():
    # No events / no dates → "—". Lets the cover template still
    # render without conditional branches.
    r = SimpleNamespace(event=None)
    assert _format_date_range([r]) == "—"


# ─── build_campaign_report_pdf (smoke + invariants) ─────────────


def _empty_engagement_recap(name="Recap"):
    """Smallest valid recap shape `build_recap_pdf_html` accepts."""
    return SimpleNamespace(
        name=name,
        approved=True,
        ambassador=None,
        job=None,
        retailer=None,
        total_engagements=None,
        products_sold=None,
        total_earnings=None,
        total_cans_sold=None,
        total_packs_sold=None,
        submited_at=None,
        event=SimpleNamespace(
            name="Sample Event",
            date=datetime(2026, 5, 10),
            start_time=None,
            end_time=None,
            address=None,
            event_type=None,
            tenant=SimpleNamespace(slug="other"),
        ),
        consumer_engagements=_RelatedList([]),
        product_samples=_RelatedList([]),
        sales_performance=_RelatedList([]),
        consumer_feedback=_RelatedList([]),
        account_feedback=_RelatedList([]),
    )


def test_build_campaign_report_pdf_rejects_empty_input():
    with pytest.raises(ValueError):
        build_campaign_report_pdf(
            title="x",
            subtitle="y",
            recaps_with_images=[],
        )


def test_build_campaign_report_pdf_renders_cover_with_title_and_count():
    recap_a = _empty_engagement_recap("Recap A")
    recap_b = _empty_engagement_recap("Recap B")

    captured = {}

    def fake_write_pdf(self, stylesheets=None):  # noqa: ARG001
        return b"%PDF-1.4 fake"

    class FakeHTML:
        def __init__(self, string):
            captured["html"] = string

        def write_pdf(self, stylesheets=None):
            return fake_write_pdf(self, stylesheets=stylesheets)

    class FakeCSS:
        def __init__(self, string):
            captured.setdefault("css", []).append(string)

    with patch("weasyprint.HTML", FakeHTML), patch("weasyprint.CSS", FakeCSS):
        out = build_campaign_report_pdf(
            title="Liquid Death · May Sampling",
            subtitle="Campaign Report",
            recaps_with_images=[(recap_a, []), (recap_b, [])],
        )

    assert out.startswith(b"%PDF")
    # Cover page renders the supplied title + subtitle and the recap
    # count text.
    html = captured["html"]
    assert "Liquid Death · May Sampling" in html
    assert "Campaign Report" in html
    assert "2 recaps" in html
    # Both recap bodies stitched into the doc, in the order supplied,
    # with explicit page breaks between detail pages.
    assert html.find("Recap A") < html.find("Recap B")
    assert html.count('class="recap-detail"') == 2
    assert "page-break-before: always" in html


def test_cover_shows_total_units_sold_and_drops_brand_aware():
    # Per Kyle's request: the cover's 4th stat card is now "Total Units
    # Sold" (summed from each recap), and the old "Brand Aware" card is
    # gone from the campaign cover.
    recap = _empty_engagement_recap("Recap A")
    recap.products_sold = 42  # legacy units-moved column

    captured = {}

    class FakeHTML:
        def __init__(self, string):
            captured["html"] = string

        def write_pdf(self, stylesheets=None):  # noqa: ARG002
            return b"%PDF-1.4 fake"

    class FakeCSS:
        def __init__(self, string):  # noqa: ARG002
            pass

    with patch("weasyprint.HTML", FakeHTML), patch("weasyprint.CSS", FakeCSS):
        build_campaign_report_pdf(
            title="Neutonic · Campaign Report",
            subtitle="Campaign Report",
            recaps_with_images=[(recap, [])],
        )

    html = captured["html"]
    # Scope assertions to the COVER (everything before the first per-recap
    # detail block). A legacy recap's own detail body still renders a
    # "Brand Aware" engagement row — Kyle only asked to drop it from the
    # cover ("1st sheet"), not from the per-recap pages.
    cover = html.split('class="recap-detail"', 1)[0]
    assert "Total Units Sold" in cover
    assert ">42<" in cover  # the summed units render on the cover
    assert "Brand Aware" not in cover


def test_build_campaign_report_pdf_singular_label_for_one_recap():
    captured = {}

    class FakeHTML:
        def __init__(self, string):
            captured["html"] = string

        def write_pdf(self, stylesheets=None):  # noqa: ARG001
            return b"%PDF-1.4 fake"

    class FakeCSS:
        def __init__(self, string):  # noqa: ARG002
            pass

    with patch("weasyprint.HTML", FakeHTML), patch("weasyprint.CSS", FakeCSS):
        build_campaign_report_pdf(
            title="Solo",
            subtitle="Sampling",
            recaps_with_images=[(_empty_engagement_recap("Solo"), [])],
        )

    assert "1 recap " in captured["html"] or "1 recap<" in captured["html"]
    assert "1 recaps" not in captured["html"]
