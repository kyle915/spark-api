"""
Coverage for HEIC → JPG display-URL resolution.

iPhone photos upload as `.heic`, which browsers can't render. The backend
server-converts each HEIC to a `.jpg` sibling blob; the GraphQL
`displayUrl` field on RecapFile / CustomRecapFile must then point the
frontend at that viewable `.jpg` (so it can use a plain <img>, no
in-browser decode / no bucket-CORS dependency).

Resolution rules (see heic_conversion.display_blob_name):
  * non-HEIC blob            → unchanged (already renderable)
  * HEIC + .jpg sibling      → the .jpg sibling path
  * HEIC + no sibling yet    → the original .heic (frontend can still
                               fall back to in-browser decoding)

These tests mock GCS existence (`blob_exists`) so they don't touch the
bucket, and assert both the pure helper and the actual async field
resolvers on the GraphQL types.
"""

import pytest
from unittest.mock import patch

from asgiref.sync import async_to_sync

from recaps import heic_conversion
from recaps import types as recap_types


# ---------------------------------------------------------------------------
# Pure helper: display_blob_name
# ---------------------------------------------------------------------------


def test_display_blob_name_non_heic_unchanged():
    # A normal image is already renderable — returned as-is, no GCS call.
    with patch.object(heic_conversion, "blob_exists") as exists:
        out = heic_conversion.display_blob_name("recaps/abc/photo.jpg")
    assert out == "recaps/abc/photo.jpg"
    exists.assert_not_called()


def test_display_blob_name_heic_with_sibling_rewrites_to_jpg():
    with patch.object(heic_conversion, "blob_exists", return_value=True):
        out = heic_conversion.display_blob_name("recaps/abc/IMG_1234.heic")
    assert out == "recaps/abc/IMG_1234.jpg"


def test_display_blob_name_heic_uppercase_ext_rewrites():
    # iPhone sometimes uploads .HEIC — case-insensitive match.
    with patch.object(heic_conversion, "blob_exists", return_value=True):
        out = heic_conversion.display_blob_name("recaps/abc/IMG_9.HEIC")
    assert out == "recaps/abc/IMG_9.jpg"


def test_display_blob_name_heif_with_sibling_rewrites():
    with patch.object(heic_conversion, "blob_exists", return_value=True):
        out = heic_conversion.display_blob_name("recaps/abc/pic.heif")
    assert out == "recaps/abc/pic.jpg"


def test_display_blob_name_heic_without_sibling_keeps_original():
    # No converted sibling in the bucket yet → keep the .heic so the
    # frontend can still try its in-browser fallback.
    with patch.object(heic_conversion, "blob_exists", return_value=False):
        out = heic_conversion.display_blob_name("recaps/abc/IMG_1234.heic")
    assert out == "recaps/abc/IMG_1234.heic"


def test_display_blob_name_none_passthrough():
    out = heic_conversion.display_blob_name(None)
    assert out is None


# ---------------------------------------------------------------------------
# GraphQL field resolvers: RecapFile.display_url / CustomRecapFile.display_url
#
# We instantiate the strawberry type and drive the async resolver directly.
# `__dict__["file"]` / `__dict__["url"]` are seeded so the resolver takes
# the fast path (no DB refresh). `blob_exists` + a bucket name are patched
# so public_url builds a deterministic storage.googleapis.com URL.
# ---------------------------------------------------------------------------


class _FakeFieldFile:
    """Mimics a Django FieldFile — `.name` is the stored blob path."""

    def __init__(self, name: str):
        self.name = name


def _resolve_recapfile_display_url(blob: str, sibling_exists: bool) -> str | None:
    inst = recap_types.RecapFile.__new__(recap_types.RecapFile)
    inst.__dict__["file"] = _FakeFieldFile(blob)
    with patch(
        "utils.gcs.blob_exists", return_value=sibling_exists
    ), patch("recaps.heic_conversion.blob_exists", return_value=sibling_exists), \
            patch("recaps.types.public_url", side_effect=lambda b: f"https://cdn/{b}" if b else None):
        return async_to_sync(inst.display_url)()


def _resolve_customfile_display_url(blob: str, sibling_exists: bool) -> str | None:
    inst = recap_types.CustomRecapFile.__new__(recap_types.CustomRecapFile)
    inst.__dict__["url"] = _FakeFieldFile(blob)
    with patch(
        "utils.gcs.blob_exists", return_value=sibling_exists
    ), patch("recaps.heic_conversion.blob_exists", return_value=sibling_exists), \
            patch("recaps.types.public_url", side_effect=lambda b: f"https://cdn/{b}" if b else None):
        return async_to_sync(inst.display_url)()


def test_recapfile_display_url_heic_with_sibling():
    out = _resolve_recapfile_display_url("recaps/IMG_1.heic", sibling_exists=True)
    assert out == "https://cdn/recaps/IMG_1.jpg"


def test_recapfile_display_url_heic_without_sibling():
    out = _resolve_recapfile_display_url("recaps/IMG_1.heic", sibling_exists=False)
    assert out == "https://cdn/recaps/IMG_1.heic"


def test_recapfile_display_url_plain_image_unchanged():
    out = _resolve_recapfile_display_url("recaps/photo.png", sibling_exists=True)
    assert out == "https://cdn/recaps/photo.png"


def test_customrecapfile_display_url_heic_with_sibling():
    out = _resolve_customfile_display_url("recaps/IMG_2.heic", sibling_exists=True)
    assert out == "https://cdn/recaps/IMG_2.jpg"


def test_customrecapfile_display_url_heic_without_sibling():
    out = _resolve_customfile_display_url("recaps/IMG_2.heic", sibling_exists=False)
    assert out == "https://cdn/recaps/IMG_2.heic"


def test_customrecapfile_display_url_plain_image_unchanged():
    out = _resolve_customfile_display_url("recaps/shot.jpeg", sibling_exists=True)
    assert out == "https://cdn/recaps/shot.jpeg"
