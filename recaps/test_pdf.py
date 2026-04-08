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
