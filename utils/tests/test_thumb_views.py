"""Coverage for the on-demand recap photo thumbnail endpoint."""

import io

import pytest
from PIL import Image

from utils import thumb_views


def _jpeg_bytes(width=1200, height=800) -> bytes:
    img = Image.new("RGB", (width, height), (90, 120, 40))
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


@pytest.fixture
def gcs(monkeypatch):
    """In-memory stand-in for the GCS helpers the view uses."""
    store: dict[str, bytes] = {"recaps/photo.jpg": _jpeg_bytes()}

    monkeypatch.setattr(
        thumb_views, "blob_exists", lambda name: name in store
    )
    monkeypatch.setattr(
        thumb_views, "download_blob_bytes", lambda name: store.get(name)
    )
    monkeypatch.setattr(
        thumb_views,
        "upload_bytes",
        lambda name, data, content_type="": store.__setitem__(name, data),
    )
    monkeypatch.setattr(
        thumb_views, "public_url", lambda name: f"https://gcs/{name}"
    )
    return store


@pytest.mark.django_db
class TestThumbEndpoint:
    URL = "/api/public/img/thumb"

    def test_generates_resizes_and_redirects(self, client, gcs):
        resp = client.get(self.URL, {"path": "recaps/photo.jpg", "w": "400"})
        assert resp.status_code == 302
        assert resp["Location"] == "https://gcs/thumbs/w400/recaps/photo.jpg.jpg"
        thumb = Image.open(
            io.BytesIO(gcs["thumbs/w400/recaps/photo.jpg.jpg"])
        )
        assert thumb.width == 400
        # Aspect ratio preserved (1200x800 → 400x266ish).
        assert 260 <= thumb.height <= 270

    def test_existing_thumb_skips_regeneration(self, client, gcs, monkeypatch):
        gcs["thumbs/w400/recaps/photo.jpg.jpg"] = b"cached"

        def boom(name):  # download must never be called
            raise AssertionError("regenerated despite cached thumb")

        monkeypatch.setattr(thumb_views, "download_blob_bytes", boom)
        resp = client.get(self.URL, {"path": "recaps/photo.jpg", "w": "400"})
        assert resp.status_code == 302
        assert "thumbs/w400" in resp["Location"]

    def test_accepts_full_public_url_and_clamps_width(self, client, gcs):
        resp = client.get(
            self.URL,
            {
                "path": "https://storage.googleapis.com/bucket/recaps/photo.jpg",
                "w": "9999",  # not whitelisted → clamps to 400
            },
        )
        assert resp.status_code == 302
        assert "thumbs/w400" in resp["Location"]

    def test_non_image_degrades_to_original(self, client, gcs):
        gcs["recaps/doc.pdf"] = b"%PDF-1.4 not an image"
        resp = client.get(self.URL, {"path": "recaps/doc.pdf", "w": "400"})
        assert resp.status_code == 302
        assert resp["Location"] == "https://gcs/recaps/doc.pdf"

    def test_small_original_is_not_upscaled(self, client, gcs):
        gcs["recaps/tiny.jpg"] = _jpeg_bytes(width=200, height=150)
        resp = client.get(self.URL, {"path": "recaps/tiny.jpg", "w": "400"})
        assert resp.status_code == 302
        assert resp["Location"] == "https://gcs/recaps/tiny.jpg"

    def test_bad_paths_rejected(self, client, gcs):
        assert client.get(self.URL, {"path": ""}).status_code == 400
        assert (
            client.get(
                self.URL, {"path": "thumbs/w400/recaps/photo.jpg.jpg"}
            ).status_code
            == 400
        )
