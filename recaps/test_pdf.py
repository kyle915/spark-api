import base64
from io import BytesIO
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from recaps.pdf import (
    SPARK_MARK_URL,
    _custom_field_sections,
    _display_answer,
    _display_field_label,
    _display_product_name,
    _spark_mark_src,
    build_recap_pdf_html,
    bytes_to_data_uri,
    detect_image_type,
    should_embed_recap_file,
)


HEIC_SAMPLE_BYTES = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"


def test_should_embed_recap_file_accepts_heic_extension():
    recap_file = SimpleNamespace(
        file_type=SimpleNamespace(extension=".heic", name="HEIC"),
        file=None,
    )

    assert should_embed_recap_file(recap_file) is True


def test_should_embed_recap_file_skips_video_with_image_file_type():
    recap_file = SimpleNamespace(
        file_type=SimpleNamespace(extension="img", name="Image"),
        url="checkin/abc/activation.MOV",
    )

    assert should_embed_recap_file(recap_file) is False


def test_should_embed_recap_file_keeps_photo_with_image_file_type():
    recap_file = SimpleNamespace(
        file_type=SimpleNamespace(extension="img", name="Image"),
        url="checkin/abc/booth.jpg",
    )

    assert should_embed_recap_file(recap_file) is True


def test_detect_image_type_identifies_heic_signature():
    assert detect_image_type(HEIC_SAMPLE_BYTES) == "heic"


def test_bytes_to_data_uri_converts_heic_to_jpeg_when_supported():
    fake_output = BytesIO()
    fake_output.write(b"\xff\xd8\xff\x00")

    class FakeImage:
        mode = "RGB"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def save(self, output, format, quality):
            output.write(fake_output.getvalue())

    with (
        patch("recaps.pdf.register_heif_opener") as mock_register_heif_opener,
        patch("recaps.pdf.Image.open", return_value=FakeImage()),
        patch("recaps.pdf.ImageOps.exif_transpose", side_effect=lambda image: image),
    ):
        data_uri = bytes_to_data_uri(HEIC_SAMPLE_BYTES)

    mock_register_heif_opener.assert_called_once()
    assert data_uri == "data:image/jpeg;base64,/9j/AA=="


class RelatedList:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


def test_custom_field_sections_skip_unanswered_fields():
    email = SimpleNamespace(name="Email Data Collection")
    engagement = SimpleNamespace(name="Consumer Engagement")

    def value(name, section, raw):
        return SimpleNamespace(
            value=raw, custom_field=SimpleNamespace(name=name, recap_section=section)
        )

    recap = SimpleNamespace(
        custom_field_value=RelatedList(
            [
                value("People engaged", engagement, "40"),
                value("Email addresses collected", email, ""),
                value("Collection method", email, "[]"),
                value("Email collection notes", email, "  "),
            ]
        )
    )

    assert _custom_field_sections(recap) == {"Consumer Engagement": [("People engaged", "40")]}


def test_products_sampled_prints_sku_names_without_catalog_type():
    results = SimpleNamespace(name="Brand Results")
    recap = SimpleNamespace(
        custom_field_value=RelatedList(
            [
                SimpleNamespace(
                    value='["Energy Drink — OG Luxe (Tropical Ice) 16oz", "Havana Nights 16oz"]',
                    custom_field=SimpleNamespace(name="Products Sampled", recap_section=results),
                ),
                SimpleNamespace(
                    value='["Full can — cold", "Sample cup"]',
                    custom_field=SimpleNamespace(name="Sample format", recap_section=results),
                ),
            ]
        )
    )

    assert _custom_field_sections(recap) == {
        "Brand Results": [
            ("Products Sampled", "OG Luxe (Tropical Ice) 16oz, Havana Nights 16oz"),
            ("Sample format", "Full can — cold, Sample cup"),
        ]
    }


def test_build_recap_pdf_html_limits_fields_for_total_wireless():
    recap = SimpleNamespace(
        name="Recap Name",
        approved=True,
        ambassador=None,
        job=SimpleNamespace(name="Job Name"),
        retailer=SimpleNamespace(name="Retailer Name"),
        total_engagements=10,
        products_sold=20,
        total_earnings="100.00",
        total_cans_sold=5,
        total_packs_sold=2,
        submited_at=None,
        event=SimpleNamespace(
            name="Store Event",
            date=datetime(2026, 4, 8, 9, 0),
            start_time=datetime(2026, 4, 8, 10, 0),
            end_time=datetime(2026, 4, 8, 14, 0),
            address="123 Main St",
            event_type=SimpleNamespace(name="Sampling"),
            tenant=SimpleNamespace(slug="total-wireless"),
        ),
        consumer_engagements=RelatedList(
            [SimpleNamespace(total_consumer=42, first_time_consumers=7)]
        ),
        product_samples=RelatedList([]),
        sales_performance=RelatedList([]),
        consumer_feedback=RelatedList(
            [
                SimpleNamespace(
                    feedback="Great response",
                    quotes="Loved it",
                    demographics="18-24",
                    positive_stories="Story",
                    reasons_to_decline="None",
                )
            ]
        ),
        account_feedback=RelatedList(
            [
                SimpleNamespace(
                    do_differently_feedback="Bring more swag",
                    feedback="Account feedback",
                    corpo_card="Card details",
                    was_corpo_card_used=True,
                )
            ]
        ),
    )

    html = build_recap_pdf_html(recap, [])

    assert "Was Corporate Card Used?" in html
    assert "Bring more swag" in html
    assert "Loved it" in html
    assert "04/08/2026" in html
    assert "Event Start" not in html
    assert "Event End" not in html
    assert "Products Sold" not in html
    assert "Ambassador Email" not in html
    assert "Submitted At" not in html
    assert "Product Samples" not in html
    assert "Sales Performance" not in html
    assert "Demographics" not in html
    assert "Positive Stories" not in html
    assert "Co-activation" not in html


def test_build_recap_pdf_html_keeps_default_fields_for_other_tenants():
    recap = SimpleNamespace(
        name="Recap Name",
        approved=False,
        ambassador=None,
        job=SimpleNamespace(name="Job Name"),
        retailer=SimpleNamespace(name="Retailer Name"),
        total_engagements=10,
        products_sold=20,
        total_earnings="100.00",
        total_cans_sold=5,
        total_packs_sold=2,
        submited_at=datetime(2026, 4, 8, 18, 0),
        event=SimpleNamespace(
            name="Store Event",
            date=datetime(2026, 4, 8, 9, 0),
            start_time="2026-04-08 10:00",
            end_time="2026-04-08 14:00",
            address="123 Main St",
            event_type=SimpleNamespace(name="Sampling"),
            tenant=SimpleNamespace(slug="another-tenant"),
        ),
        consumer_engagements=RelatedList([SimpleNamespace(total_consumer=42)]),
        product_samples=RelatedList([]),
        sales_performance=RelatedList([]),
        consumer_feedback=RelatedList([SimpleNamespace(feedback="Great response")]),
        account_feedback=RelatedList([SimpleNamespace(corpo_card="Card details")]),
    )

    html = build_recap_pdf_html(recap, [])

    assert "04/08/2026" in html
    assert "2026-04-08 09:00" not in html
    assert "2026-04-08 18:00" not in html
    assert "Products Sold" in html
    assert "Submitted At" in html
    assert "Product Samples" in html
    assert "Sales Performance" in html
    assert "Corpo Card" in html
    assert "Was Corporate Card Used?" not in html


def test_event_activation_custom_recap_skips_empty_sales_card():
    def recap(template_name, sales):
        return SimpleNamespace(
            name="Fest",
            approved=True,
            ambassador=None,
            custom_recap_template=SimpleNamespace(
                name=template_name,
                event_type=None,
                custom_field=RelatedList(
                    [SimpleNamespace(name="How many packs did consumers purchase?")]
                ),
            ),
            total_engagements=312,
            event=SimpleNamespace(
                name="Fest",
                date=datetime(2026, 9, 26, 14, 0),
                tenant=SimpleNamespace(slug="torch-thc"),
            ),
            custom_recap_product_sample=RelatedList([]),
            custom_recap_sale_performance=RelatedList(sales),
            custom_field_value=RelatedList([]),
        )

    activation = "Torch THC-Event Activation"
    assert "Sales Performance" not in build_recap_pdf_html(recap(activation, []), [])
    assert "Sales Performance" in build_recap_pdf_html(recap("Torch THC-Retail Sampling", []), [])
    sale = SimpleNamespace(
        product=SimpleNamespace(name="Can"), type_of_good=SimpleNamespace(name="Single"), price=5
    )
    assert "Sales Performance" in build_recap_pdf_html(recap(activation, [sale]), [])


def _activation_recap(*, layout, samples, values=()):
    section = SimpleNamespace(name="Feedback & Account Notes")
    return SimpleNamespace(
        name="Fest",
        approved=True,
        ambassador=None,
        custom_recap_template=SimpleNamespace(
            name="Torch THC-Event Activation", event_type=None, layout=layout
        ),
        total_engagements=312,
        event=SimpleNamespace(
            name="Fest",
            date=datetime(2026, 9, 26, 14, 0),
            tenant=SimpleNamespace(slug="torch-thc"),
        ),
        custom_recap_product_sample=RelatedList(
            [
                SimpleNamespace(product=SimpleNamespace(name=name), quantity=qty)
                for name, qty in samples
            ]
        ),
        custom_recap_sale_performance=RelatedList([]),
        custom_field_value=RelatedList(
            [
                SimpleNamespace(
                    value=value,
                    custom_field=SimpleNamespace(name=label, recap_section=section),
                )
                for label, value in values
            ]
        ),
    )


def test_custom_recap_pdf_labels_per_sku_cans_from_template_layout():
    samples = [("Black Cherry 5mg 12oz", 96), ("Blue Razz 25mg 12oz", 1200)]
    layout = {"sampleQtyLabel": "Cans sampled", "sampleQtyTotalLabel": "Total cans sampled"}
    html = build_recap_pdf_html(_activation_recap(layout=layout, samples=samples), [])
    assert "Black Cherry 5mg 12oz · Cans sampled: 96" in html
    assert "Total cans sampled: 1,296" in html
    assert "Qty:" not in html

    plain = build_recap_pdf_html(_activation_recap(layout={}, samples=samples), [])
    assert "Black Cherry 5mg 12oz - Qty: 96" in plain
    assert "Cans sampled" not in plain


def test_display_answer_keeps_prose_dashes():
    assert _display_answer("Great crowd - lots of families after 6pm") == (
        "Great crowd - lots of families after 6pm"
    )
    assert _display_answer('"Tastes like a real seltzer." - female, ~30') == (
        '"Tastes like a real seltzer." - female, ~30'
    )
    assert _display_answer("Line one - ok\nLINE TWO - FINE") == "Line one - ok\nLINE TWO - FINE"
    assert _display_answer("SPARKLING WATER - STRAWBERRY TERROR") == (
        "Sparkling water · Strawberry Terror"
    )
    assert _display_answer("JUST SHOUTING TEXT") == "Just Shouting Text"


def test_custom_recap_pdf_prose_answer_keeps_spaced_dash():
    values = [("Consumer Feedback", "Black Cherry won - people loved the 5mg")]
    html = build_recap_pdf_html(_activation_recap(layout={}, samples=[], values=values), [])
    assert "Black Cherry won - people loved the 5mg" in html
    assert "Black Cherry won · people" not in html


def test_build_recap_pdf_html_groups_custom_fields_by_recap_section():
    account_section = SimpleNamespace(name="Account Feedback")
    consumer_section = SimpleNamespace(name="Consumer Feedback")
    recap = SimpleNamespace(
        name="Custom Recap Name",
        approved=True,
        ambassador=None,
        job=SimpleNamespace(name="Job Name"),
        retailer=SimpleNamespace(name="Retailer Name"),
        location=SimpleNamespace(name="Miami"),
        state=SimpleNamespace(name="State Name"),
        tenant=SimpleNamespace(name="Tenant Name"),
        timezone=SimpleNamespace(name="Central"),
        custom_recap_template=SimpleNamespace(name="Template Name"),
        total_engagements=10,
        filling_for_ambassador=True,
        late=False,
        incomplete=False,
        used_corpo_card=True,
        submitted_at=datetime(2026, 4, 8, 18, 0),
        created_at=datetime(2026, 4, 8, 19, 0),
        updated_at=datetime(2026, 4, 8, 20, 0),
        event=SimpleNamespace(
            name="Store Event",
            date=datetime(2026, 4, 8, 9, 0),
            start_time="2026-04-08 10:00",
            end_time="2026-04-08 14:00",
            address="123 Main St",
            event_type=SimpleNamespace(name="Sampling"),
            tenant=SimpleNamespace(slug="another-tenant"),
        ),
        custom_recap_product_sample=RelatedList([]),
        custom_recap_sale_performance=RelatedList([]),
        custom_field_value=RelatedList(
            [
                SimpleNamespace(
                    value="Bring more signage",
                    custom_field=SimpleNamespace(
                        name="Do Differently",
                        recap_section=account_section,
                    ),
                ),
                SimpleNamespace(
                    value="Great comments",
                    custom_field=SimpleNamespace(
                        name="Feedback",
                        recap_section=consumer_section,
                    ),
                ),
            ]
        ),
    )

    html = build_recap_pdf_html(recap, [])

    assert "Account Feedback" in html
    assert "Do Differently" in html
    assert "Bring more signage" in html
    assert "Consumer Feedback" in html
    assert "Great comments" in html
    assert "<h2>Custom Fields</h2>" not in html
    # Client-facing summary: no internal bookkeeping fields, no BA email.
    assert "Used Corpo Card" not in html
    assert "Timezone" not in html
    assert "Ambassador Email" not in html
    assert "Miami" in html
    assert "Submitted At" not in html
    assert "Filling For Ambassador" not in html
    assert "Late" not in html
    assert "Incomplete" not in html
    assert "APPROVED" in html
    assert "Job" not in html
    assert "Location" not in html
    assert "Tenant" not in html
    assert "Template Name" not in html
    assert "Custom Recap Template" not in html
    assert "Created At" not in html
    assert "Updated At" not in html
    # No sample/sales toggles or fields on this template, so no N/A cards.
    # The template-asks-for-it side lives in
    # recaps/tests/test_file_category_and_pdf_samples.py.
    assert "Product Samples" not in html
    assert "Sales Performance" not in html
    assert 'class="spark-mark"' in html
    assert 'alt="Spark by Ignite"' in html
    assert "data:image/png;base64," in html or SPARK_MARK_URL in html
    assert 'Spark <span class="lime">by Ignite</span>' not in html


def test_spark_mark_stays_dark_on_light_pdf():
    """White recap PDF must use the black mark — never invert to white-on-white."""
    _spark_mark_src.cache_clear()
    dark_mark = _spark_mark_src(invert=False)
    inverted = _spark_mark_src(invert=True)
    recap = SimpleNamespace(
        name="Recap Name",
        approved=False,
        ambassador=None,
        job=None,
        retailer=None,
        total_engagements=1,
        products_sold=None,
        total_earnings=None,
        total_cans_sold=None,
        total_packs_sold=None,
        submited_at=None,
        event=SimpleNamespace(
            name="Store Event",
            date=datetime(2026, 8, 16, 9, 0),
            tenant=SimpleNamespace(slug="liquid-death"),
        ),
        consumer_engagements=RelatedList([]),
        product_samples=RelatedList([]),
        sales_performance=RelatedList([]),
        consumer_feedback=RelatedList([]),
        account_feedback=RelatedList([]),
    )
    html = build_recap_pdf_html(recap, [])
    assert dark_mark in html or SPARK_MARK_URL in html
    if inverted.startswith("data:") and inverted != dark_mark:
        assert inverted not in html


def test_display_field_label_sentence_cases_and_fixes_tasing():
    assert (
        _display_field_label("TOTAL NUMBER OF CONSUMERS SAMPLED")
        == "Total number of consumers sampled"
    )
    assert (
        _display_field_label(
            "HOW MANY CONSUMERS WOULD BE WILLING TO PURCHASE THE PRODUCT AFTER TASING IT?"
        )
        == "How many consumers would be willing to purchase the product after tasting it?"
    )
    assert _display_field_label("First-time consumers") == "First-time consumers"


def test_display_product_name_keeps_flavor_identity():
    assert (
        _display_product_name("SPARKLING WATER - STRAWBERRY TERROR")
        == "Sparkling water · Strawberry Terror"
    )
    assert _display_product_name("LIQUID DEATH") == "Liquid Death"
    assert _display_product_name("Strawberry Terror") == "Strawberry Terror"


# Tiny but valid JPEG: detect_image_type() only sniffs the SOI marker
# (\xff\xd8\xff) and bytes_to_data_uri() base64-encodes JPEG bytes as-is
# (no PIL decode), so this is enough to exercise the embed path.
JPEG_SAMPLE_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF"
JPEG_SAMPLE_DATA_URI = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="


def test_build_recap_pdf_html_embeds_image_custom_field_value():
    """An image-type custom field renders its photo inline, not the blob
    path. Regression for receipts showing as `recaps/receipts/<uuid>.jpg`
    text instead of the actual image."""
    receipts_section = SimpleNamespace(name="Receipts")
    notes_section = SimpleNamespace(name="Notes")
    receipt_blob_path = "recaps/receipts/abc-123.jpg"
    recap = SimpleNamespace(
        name="Custom Recap Name",
        approved=True,
        ambassador=None,
        location=SimpleNamespace(name="Miami"),
        state=SimpleNamespace(name="State Name"),
        retailer=SimpleNamespace(name="Retailer Name"),
        timezone=SimpleNamespace(name="Central"),
        total_engagements=10,
        used_corpo_card=True,
        custom_recap_template=SimpleNamespace(name="Template Name"),
        event=SimpleNamespace(
            name="Store Event",
            date=datetime(2026, 4, 8, 9, 0),
            tenant=SimpleNamespace(slug="liquid-death"),
        ),
        custom_recap_product_sample=RelatedList([]),
        custom_recap_sale_performance=RelatedList([]),
        custom_field_value=RelatedList(
            [
                SimpleNamespace(
                    value=receipt_blob_path,
                    custom_field=SimpleNamespace(
                        name="Product Purchase Receipt (Image)",
                        recap_section=receipts_section,
                    ),
                ),
                SimpleNamespace(
                    value="Sampled 200 cans",
                    custom_field=SimpleNamespace(
                        name="Summary",
                        recap_section=notes_section,
                    ),
                ),
            ]
        ),
    )

    html = build_recap_pdf_html(
        recap,
        images=[],
        custom_field_images={receipt_blob_path: JPEG_SAMPLE_BYTES},
    )

    # Image field: embedded as an <img> data URI, label kept as caption.
    assert JPEG_SAMPLE_DATA_URI in html
    assert "Product Purchase Receipt (Image)" in html
    assert f'<img src="{JPEG_SAMPLE_DATA_URI}"' in html
    # The raw blob path must never appear as visible text.
    assert receipt_blob_path not in html
    # Non-image field still renders as plain text.
    assert "<p>Sampled 200 cans</p>" in html


def test_build_recap_pdf_html_image_field_falls_back_to_text_when_unfetched():
    """If a blob couldn't be fetched (not in custom_field_images), the
    field renders as text rather than crashing or dropping silently."""
    section = SimpleNamespace(name="Receipts")
    blob_path = "recaps/receipts/missing.jpg"
    recap = SimpleNamespace(
        name="Custom Recap Name",
        approved=True,
        ambassador=None,
        location=SimpleNamespace(name="Miami"),
        state=SimpleNamespace(name="State Name"),
        retailer=SimpleNamespace(name="Retailer Name"),
        timezone=SimpleNamespace(name="Central"),
        total_engagements=10,
        used_corpo_card=True,
        custom_recap_template=SimpleNamespace(name="Template Name"),
        event=SimpleNamespace(
            name="Store Event",
            date=datetime(2026, 4, 8, 9, 0),
            tenant=SimpleNamespace(slug="liquid-death"),
        ),
        custom_recap_product_sample=RelatedList([]),
        custom_recap_sale_performance=RelatedList([]),
        custom_field_value=RelatedList(
            [
                SimpleNamespace(
                    value=blob_path,
                    custom_field=SimpleNamespace(
                        name="Receipt (Image)",
                        recap_section=section,
                    ),
                ),
            ]
        ),
    )

    # No custom_field_images passed → legacy behavior (text).
    html = build_recap_pdf_html(recap, images=[])

    assert f'<img src="{blob_path}"' not in html
    assert "<p>" + blob_path + "</p>" in html
    assert blob_path in html


def test_build_recap_pdf_html_downscales_full_res_photos():
    """Photo-heavy recap PDFs used to inline every photo at full resolution
    (3000-4000px / multi-MB), so a recap with many photos produced a 50MB+
    download and a slow, timeout-prone render. Photos are now downscaled to
    <=1400px @ q80 before base64-embedding, mirroring the campaign report."""
    import base64
    import os
    import re

    from PIL import Image as PILImage

    # A real full-res-ish JPEG. Random noise (not a flat color, which JPEG
    # would crush to a few KB) so the source is genuinely large, like a photo.
    big = BytesIO()
    PILImage.frombytes(
        "RGB", (3000, 3000), os.urandom(3000 * 3000 * 3)
    ).save(big, format="JPEG", quality=95)
    big_bytes = big.getvalue()
    assert len(big_bytes) > 500_000  # sanity: a chunky source image (MB-scale)

    recap = SimpleNamespace(
        name="Photo Recap",
        approved=True,
        ambassador=None,
        location=SimpleNamespace(name="Tempe"),
        state=SimpleNamespace(name="AZ"),
        retailer=SimpleNamespace(name="Total Wireless"),
        timezone=SimpleNamespace(name="Mountain"),
        total_engagements=0,
        used_corpo_card=False,
        custom_recap_template=SimpleNamespace(name="Template"),
        event=SimpleNamespace(
            name="Store Event",
            date=datetime(2026, 8, 8, 9, 0),
            tenant=SimpleNamespace(slug="total-wireless"),
        ),
        custom_recap_product_sample=RelatedList([]),
        custom_recap_sale_performance=RelatedList([]),
        custom_field_value=RelatedList([]),
    )

    html = build_recap_pdf_html(
        recap, images=[{"bytes": big_bytes, "category": "Photos"}]
    )

    # Pull the embedded JPEG data URI back out and decode it.
    match = re.search(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", html)
    assert match, "expected an embedded photo data URI"
    embedded = base64.b64decode(match.group(1))

    # Downscaled: longest side capped at 1400px, and materially smaller bytes.
    with PILImage.open(BytesIO(embedded)) as im:
        assert max(im.size) <= 1400
    assert len(embedded) < len(big_bytes)


def _share_recap(*, approved: bool):
    return SimpleNamespace(
        name="Shared Recap",
        approved=approved,
        ambassador=None,
        location=SimpleNamespace(name="Austin"),
        state=SimpleNamespace(name="TX"),
        retailer=SimpleNamespace(name="HEB"),
        timezone=SimpleNamespace(name="Central"),
        total_engagements=10,
        used_corpo_card=False,
        custom_recap_template=SimpleNamespace(name="Template"),
        event=SimpleNamespace(
            name="Store Event",
            date=datetime(2026, 8, 8, 9, 0),
            tenant=SimpleNamespace(name="Girl Beer", slug="girl-beer"),
        ),
        custom_recap_product_sample=RelatedList([]),
        custom_recap_sale_performance=RelatedList([]),
        custom_field_value=RelatedList([]),
    )


def test_public_share_pdf_omits_draft_chip():
    html = build_recap_pdf_html(_share_recap(approved=False), [], public_share=True)
    assert "DRAFT" not in html
    assert "Girl Beer" in html
    assert "Shared recap" in html
    assert "badge-draft" not in html
    assert 'class="spark-mark"' in html
    assert 'Spark <span class="lime">by Ignite</span>' not in html


def test_custom_field_pdf_sentence_cases_tasing_label():
    recap = _share_recap(approved=True)
    recap.custom_field_value = RelatedList(
        [
            SimpleNamespace(
                value="12",
                custom_field=SimpleNamespace(
                    name=(
                        "HOW MANY CONSUMERS WOULD BE WILLING TO PURCHASE "
                        "THE PRODUCT AFTER TASING IT?"
                    ),
                    recap_section=SimpleNamespace(name="Consumer Engagement"),
                ),
            )
        ]
    )
    html = build_recap_pdf_html(recap, [])
    assert "tasting" in html
    assert "TASING" not in html
    assert "How many consumers would be willing to purchase" in html


def test_public_share_pdf_keeps_approved_chip():
    html = build_recap_pdf_html(_share_recap(approved=True), [], public_share=True)
    assert "APPROVED" in html
    assert "DRAFT" not in html
    assert "Girl Beer" in html


def test_internal_pdf_still_prints_draft_chip():
    html = build_recap_pdf_html(_share_recap(approved=False), [])
    assert "DRAFT" in html
    assert "badge-draft" in html


def test_torch_retail_pdf_sales_card_counts_cans_and_packs_as_one_unit_each():
    recap = _activation_recap(
        layout={},
        samples=[],
        values=[
            ("How many single cans did consumers purchase?", "3"),
            ("How many packs did consumers purchase?", "2"),
            ("Account Spend Amount", "45"),
        ],
    )
    recap.custom_recap_template.name = "Torch THC-Retail Sampling"
    html = build_recap_pdf_html(recap, [])
    sales_card = html.split("<h2>Sales Performance</h2>", 1)[1].split("</section>", 1)[0]
    assert "How many single cans did consumers purchase?: 3" in sales_card
    assert "How many packs did consumers purchase?: 2" in sales_card
    assert "Total units sold: 5" in sales_card
    assert "45" not in sales_card


# ─── Client-facing presentation ─────────────────────────────────────


def test_client_safe_ba_name_never_leaks_contact_details():
    from recaps.pdf import client_safe_ba_name

    assert client_safe_ba_name("Adaiah", "Dotson") == "Adaiah D."
    assert client_safe_ba_name("Elmer", "") == "Elmer"
    assert client_safe_ba_name("", "", "checkin-9516271509@walkup.spark") == ""
    assert client_safe_ba_name("", "", "(951) 627-1509") == ""
    # A last-name-only record is reduced to an initial, never the surname.
    assert client_safe_ba_name("", "Dotson") == "D."


def test_ba_name_custom_field_is_masked_like_the_summary_row():
    section = SimpleNamespace(name="Event Details")
    recap = SimpleNamespace(
        custom_field_value=RelatedList(
            [
                SimpleNamespace(
                    value="Adaiah Marie Dotson",
                    custom_field=SimpleNamespace(name="BA Name", recap_section=section),
                ),
                SimpleNamespace(
                    value="Jordan Rivera",
                    custom_field=SimpleNamespace(
                        name="Brand Ambassador's Full Name", recap_section=section
                    ),
                ),
                SimpleNamespace(
                    value="Binny's Lincoln Park",
                    custom_field=SimpleNamespace(name="Store Name", recap_section=section),
                ),
            ]
        )
    )

    assert _custom_field_sections(recap) == {
        "Event Details": [
            ("BA Name", "Adaiah D."),
            ("Brand Ambassador's Full Name", "Jordan R."),
            ("Store Name", "Binny's Lincoln Park"),
        ]
    }


def test_format_answer_number_formats_money_and_counts_only():
    from recaps.pdf import format_answer_number

    assert format_answer_number("Estimated sales $", "125342.00") == "$125,342.00"
    assert format_answer_number("Costco retail price: Almond Oil", "1279") == "$1,279"
    assert format_answer_number("Total number of consumers sampled", "4000") == "4,000"
    assert format_answer_number("Units sold: Almond Oil", "44") == "44"
    assert (
        format_answer_number("How many were first-time consumers?", "4000") == "4,000"
    )
    # Not counts: left exactly as entered.
    assert format_answer_number("Store number", "1715") == "1715"
    assert format_answer_number("Zip code", "92346") == "92346"
    assert format_answer_number("Sell-out time", "5:00pm") == "5:00pm"
    assert format_answer_number("Foot traffic level", "4") == "4"
    assert format_answer_number("Any feedback?", "No") == "No"


def _refresh_recap(values, *, name="10/11/2026 - 28000 Greenspot Road, Highland"):
    section = SimpleNamespace(name="Sampling")
    user = SimpleNamespace(
        first_name="Adaiah",
        last_name="Dotson",
        email="checkin-9516271509@walkup.spark",
        username="checkin-9516271509",
    )
    return SimpleNamespace(
        name=name,
        approved=True,
        ambassador=SimpleNamespace(user=user),
        external_ba_name="",
        retailer=None,
        location=SimpleNamespace(name="Highland"),
        state=SimpleNamespace(name="California"),
        tenant=SimpleNamespace(name="Fresh Vintage Farms"),
        timezone=None,
        custom_recap_template=SimpleNamespace(name="Costco Roadshow Recap"),
        total_engagements=4000,
        used_corpo_card=True,
        source_label="",
        event=SimpleNamespace(
            name=name,
            date=datetime(2026, 10, 11, 9, 0),
            address="28000 Greenspot Road",
            retailer=None,
            state=None,
            tenant=SimpleNamespace(name="Fresh Vintage Farms", slug="fresh-vintage"),
        ),
        custom_field_value=SimpleNamespace(
            all=lambda: [
                SimpleNamespace(
                    value=value,
                    custom_field=SimpleNamespace(name=label, recap_section=section),
                )
                for label, value in values
            ]
        ),
    )


def test_custom_recap_pdf_is_client_facing_with_headline():
    recap = _refresh_recap(
        [
            ("Total number of consumers sampled", "4000"),
            ("Units sold: Almond Oil", "44"),
            ("Units sold: Garlic Almond Oil", "54"),
        ]
    )
    html = build_recap_pdf_html(recap, [])

    # Ambassador: first name + last initial; no email, no phone.
    assert "Adaiah D." in html
    assert "Dotson" not in html
    assert "walkup.spark" not in html
    assert "9516271509" not in html
    assert "Ambassador Email" not in html
    # Title drops the auto-namer's leading date; the date prints underneath.
    assert "<h1>28000 Greenspot Road, Highland</h1>" in html
    assert "October 11, 2026" in html
    # Headline strip.
    assert 'class="headline"' in html
    assert "<strong>4,000</strong><span>Sampled</span>" in html
    assert "<strong>98</strong><span>Sold</span>" in html
    assert "<strong>2.5%</strong><span>Conversion</span>" in html
    # No N/A filler in the summary.
    assert "<strong>N/A</strong>" not in html


def test_custom_recap_pdf_photos_lead_and_receipts_trail():
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    recap = _refresh_recap([("Total number of consumers sampled", "120")])
    html = build_recap_pdf_html(
        recap,
        [
            {"name": "Web check-in photo", "bytes": png, "category": "Expense Receipts"},
            {"name": "Web check-in photo", "bytes": png, "category": "Full booth setup"},
        ],
    )
    assert "<h2>Photos</h2>" in html and "<h2>Receipts</h2>" in html
    assert html.index("<h2>Photos</h2>") < html.index("<h2>Sampling</h2>")
    assert html.index("<h2>Sampling</h2>") < html.index("<h2>Receipts</h2>")
    # Captioned by category, not by the upload's file name.
    assert "<figcaption>Full booth setup</figcaption>" in html
    assert "Web check-in photo" not in html


def test_custom_recap_pdf_has_one_sales_performance_card():
    section = SimpleNamespace(name="Sales Performance")
    recap = _refresh_recap([])
    recap.custom_field_value = SimpleNamespace(
        all=lambda: [
            SimpleNamespace(
                value="44",
                custom_field=SimpleNamespace(
                    name="Units sold: Almond Oil", recap_section=section
                ),
            )
        ]
    )
    html = build_recap_pdf_html(recap, [])
    assert html.count("<h2>Sales Performance</h2>") == 1
