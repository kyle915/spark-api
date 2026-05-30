from io import BytesIO
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from recaps.pdf import (
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
    assert "Used Corpo Card" in html
    assert "City" in html
    assert "Miami" in html
    assert html.index("State") < html.index("City") < html.index("Retailer")
    assert "Submitted At" not in html
    assert "Filling For Ambassador" not in html
    assert "Late" not in html
    assert "Incomplete" not in html
    assert "Approved" in html
    assert "Job" not in html
    assert "Location" not in html
    assert "Tenant" not in html
    assert "Template Name" not in html
    assert "Custom Recap Template" not in html
    assert "Created At" not in html
    assert "Updated At" not in html
    assert "Product Samples" not in html
    assert "Sales Performance" not in html


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

    assert "<img" not in html
    assert blob_path in html
