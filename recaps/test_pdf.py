from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from recaps.pdf import bytes_to_data_uri, detect_image_type, should_embed_recap_file


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
