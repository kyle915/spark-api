"""Private storage with signed reads (utils/private_media.py + media views).

Flag OFF must be byte-identical to the old public URLs. Flag ON: private keys
become /api/public/media/o/<token>/<name>; the endpoint verifies the HMAC and
302s to a (cached) signed URL; the legacy ?path= endpoints refuse bare
private keys; URL minting never touches GCS/IAM.
"""

from __future__ import annotations

import time

import pytest
from django.test import override_settings

from utils import gcs, private_media
from utils.gcs import extract_blob_name_from_url, public_url
from utils.private_media import (
    TokenError,
    TokenExpired,
    is_public_blob,
    media_url,
    mint_token,
    token_from_url,
    verify_token,
)

API = "https://api.test"
ON = dict(
    GS_BUCKET_NAME="bkt",
    PRIVATE_MEDIA_ENABLED=True,
    PRIVATE_MEDIA_DIRECT_PUBLIC_PREFIXES=True,
    PRIVATE_MEDIA_BASE_URL=API,
    PRIVATE_MEDIA_TOKEN_KEYS="k1:first-secret",
)


@pytest.fixture(autouse=True)
def _clean_cache():
    private_media.clear_signed_cache()
    yield
    private_media.clear_signed_cache()


@pytest.fixture
def no_signing(monkeypatch):
    """Fail loudly if anything tries to sign (network) while minting URLs."""

    def boom(*a, **k):
        raise AssertionError("URL minting must not sign / call IAM")

    monkeypatch.setattr(gcs, "generate_download_url", boom)
    monkeypatch.setattr(gcs, "_iam_signing_args_for_default_credentials", boom)


@pytest.fixture
def fake_signer(monkeypatch):
    calls = []

    def sign(blob, expiration_minutes=60, *, response_type=None, response_disposition=None):
        calls.append((blob, expiration_minutes, response_type, response_disposition))
        return f"https://storage.googleapis.com/bkt/{blob}?X-Goog-Signature=sig{len(calls)}"

    monkeypatch.setattr(gcs, "generate_download_url", sign)
    return calls


# --------------------------------------------------------------------------
# Classification + URL building
# --------------------------------------------------------------------------
class TestClassification:
    @pytest.mark.parametrize(
        "key,public",
        [
            ("products/a.jpg", True),
            ("tenants/images/logo.png", True),
            ("receipt-campaign-images/hero.png", True),
            ("thumbs/w400/products/a.jpg.jpg", True),
            ("thumbs/w400/recaps/a.jpg.jpg", False),
            ("recaps/a.jpg", False),
            ("recaps/receipts/r.jpg", False),
            ("recap_files/checkin/x/a.jpg", False),
            ("documents/id.pdf", False),
            ("ba/resume.pdf", False),
            ("users/images/u.png", False),
            ("chat_attachments/c.png", False),
            ("connecteam-imports/x.pdf", False),
            ("consumer-receipts/1/2/x.jpg", False),
            ("uploads/x.png", False),
            ("somethingnew/x.png", False),  # unknown prefixes fail closed
            ("products/../documents/x.pdf", False),
        ],
    )
    def test_prefixes(self, key, public):
        assert is_public_blob(key) is public


class TestUrlBuilding:
    @override_settings(GS_BUCKET_NAME="bkt", PRIVATE_MEDIA_ENABLED=False)
    def test_flag_off_is_byte_identical(self, no_signing):
        assert public_url("recaps/a.jpg") == "https://storage.googleapis.com/bkt/recaps/a.jpg"
        assert public_url("/documents/x.pdf") == "https://storage.googleapis.com/bkt/documents/x.pdf"
        assert public_url("recaps/a.jpg", durable=True) == "https://storage.googleapis.com/bkt/recaps/a.jpg"
        assert public_url(None) is None

    @override_settings(GS_BUCKET_NAME="", PRIVATE_MEDIA_ENABLED=True)
    def test_no_bucket_still_none(self):
        assert public_url("recaps/a.jpg") is None

    @override_settings(**ON)
    def test_private_key_gets_media_url_with_filename(self, no_signing):
        url = public_url("recaps/abc/IMG 1.HEIC")
        assert url.startswith(f"{API}/api/public/media/o/")
        assert url.endswith("/IMG%201.HEIC")  # extension preserved for clients
        assert "recaps" not in url  # the key itself is not in the URL
        assert verify_token(token_from_url(url)) == "recaps/abc/IMG 1.HEIC"

    @override_settings(**ON)
    def test_public_prefix_stays_direct(self, no_signing):
        assert public_url("products/a.jpg") == "https://storage.googleapis.com/bkt/products/a.jpg"

    @override_settings(**{**ON, "PRIVATE_MEDIA_DIRECT_PUBLIC_PREFIXES": False})
    def test_public_prefix_tokenized_and_durable_when_direct_off(self, no_signing):
        url = public_url("products/a.jpg")
        assert "/api/public/media/o/" in url
        far_future = time.time() + 400 * 24 * 3600
        assert verify_token(token_from_url(url), now=far_future) == "products/a.jpg"

    @override_settings(**ON)
    def test_interactive_url_stable_within_window(self):
        assert public_url("recaps/a.jpg") == public_url("recaps/a.jpg")

    @override_settings(**ON)
    def test_unsafe_keys_refused(self):
        assert media_url("recaps/../documents/x.pdf") is None
        assert media_url("recaps/a\\b.jpg") is None


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
class TestTokens:
    @override_settings(**ON)
    def test_interactive_expires_after_one_to_two_windows(self):
        now = 1_800_000_000.0
        tok = mint_token("recaps/a.jpg", now=now)
        assert verify_token(tok, now=now + 11 * 3600) == "recaps/a.jpg"
        with pytest.raises(TokenExpired):
            verify_token(tok, now=now + 25 * 3600)

    @override_settings(**ON)
    def test_durable_never_expires(self):
        tok = mint_token("recaps/a.jpg", durable=True)
        assert verify_token(tok, now=time.time() + 10 * 365 * 86400) == "recaps/a.jpg"

    @override_settings(**ON)
    @pytest.mark.parametrize("mutate", ["payload", "sig", "kid", "shape"])
    def test_tampering_rejected(self, mutate):
        kid, payload, sig = mint_token("recaps/a.jpg").split(".")
        other_kid, other_payload, _ = mint_token("documents/secret.pdf").split(".")
        bad = {
            "payload": f"{kid}.{other_payload}.{sig}",
            "sig": f"{kid}.{payload}.{sig[:-2]}AA",
            "kid": f"zz.{payload}.{sig}",
            "shape": f"{payload}.{sig}",
        }[mutate]
        with pytest.raises(TokenError):
            verify_token(bad)

    def test_rotation(self):
        with override_settings(**{**ON, "PRIVATE_MEDIA_TOKEN_KEYS": "k1:old"}):
            old = mint_token("recaps/a.jpg", durable=True)
        # Prepend a new key: old links still verify, new ones use k2.
        with override_settings(**{**ON, "PRIVATE_MEDIA_TOKEN_KEYS": "k2:new,k1:old"}):
            assert verify_token(old) == "recaps/a.jpg"
            assert mint_token("recaps/a.jpg").startswith("k2.")
        # Drop k1: every link it signed (durable included) is revoked.
        with override_settings(**{**ON, "PRIVATE_MEDIA_TOKEN_KEYS": "k2:new"}):
            with pytest.raises(TokenError):
                verify_token(old)

    @override_settings(**{**ON, "PRIVATE_MEDIA_TOKEN_KEYS": ""}, SECRET_KEY="abc")
    def test_default_key_derived_from_secret_key(self):
        assert mint_token("recaps/a.jpg").startswith("s0.")


class TestExtractBlobName:
    @override_settings(**ON)
    def test_media_url_decodes_back_to_key(self):
        url = public_url("recaps/abc/a.jpg")
        assert extract_blob_name_from_url(url) == "recaps/abc/a.jpg"

    @override_settings(**ON)
    def test_expired_media_url_still_decodes_for_writes(self):
        tok = mint_token("ba/photo.jpg", now=time.time() - 30 * 86400)
        assert extract_blob_name_from_url(f"{API}/api/public/media/o/{tok}/photo.jpg") == "ba/photo.jpg"

    @override_settings(**ON)
    def test_forged_media_url_yields_none(self):
        assert extract_blob_name_from_url(f"{API}/api/public/media/o/k1.AAAA.BBBB/x.jpg") is None

    @override_settings(**ON)
    def test_legacy_forms_unchanged(self):
        assert extract_blob_name_from_url("https://storage.googleapis.com/bkt/recaps/a.jpg") == "recaps/a.jpg"
        assert extract_blob_name_from_url("recaps/a.jpg") == "recaps/a.jpg"


# --------------------------------------------------------------------------
# Media endpoint
# --------------------------------------------------------------------------
@pytest.mark.django_db
class TestMediaEndpoint:
    def _path(self, blob, **kw):
        url = media_url(blob, **kw)
        return url[len(API):]

    @override_settings(**ON)
    def test_valid_token_redirects_to_signed_url(self, client, fake_signer):
        resp = client.get(self._path("recaps/abc/a.jpg"))
        assert resp.status_code == 302
        assert resp["Location"].startswith("https://storage.googleapis.com/bkt/recaps/abc/a.jpg?X-Goog-Signature=")
        assert resp["Cache-Control"].startswith("private, max-age=")
        assert 0 < int(resp["Cache-Control"].split("=")[1]) <= 1800
        assert resp["Referrer-Policy"] == "no-referrer"
        assert fake_signer[0][1] == 60  # signed TTL minutes

    @override_settings(**ON)
    def test_signature_cached_per_blob(self, client, fake_signer):
        path = self._path("recaps/abc/a.jpg")
        first = client.get(path)["Location"]
        second = client.get(path)["Location"]
        assert first == second
        assert len(fake_signer) == 1

    @override_settings(**ON)
    def test_expired_is_410_and_bad_is_404(self, client, fake_signer):
        tok = mint_token("recaps/a.jpg", now=time.time() - 3 * 86400)
        assert client.get(f"/api/public/media/o/{tok}/a.jpg").status_code == 410
        assert client.get("/api/public/media/o/k1.AAAA.BBBB/a.jpg").status_code == 404
        assert fake_signer == []

    @override_settings(**ON)
    def test_video_gets_inline_mp4(self, client, fake_signer):
        client.get(self._path("recap_files/checkin/e/clip.mov"))
        assert fake_signer[0][2:] == ("video/mp4", "inline")

    @override_settings(**ON)
    def test_heic_requested_as_jpg_serves_sibling(self, client, fake_signer):
        url = self._path("recaps/abc/IMG_1.heic")
        client.get(url[: -len(".heic")] + ".jpg")
        assert fake_signer[-1][0] == "recaps/abc/IMG_1.jpg"
        # Any other renamed suffix still serves the signed key itself.
        client.get(url[: -len("IMG_1.heic")] + "passwords.pdf")
        assert fake_signer[-1][0] == "recaps/abc/IMG_1.heic"

    @override_settings(**ON)
    def test_thumbnail_param(self, client, fake_signer, monkeypatch):
        from utils import thumb_views

        seen = {}

        def fake_resolve(blob, width):
            seen["args"] = (blob, width)
            return f"thumbs/w{width}/{blob}.jpg"

        monkeypatch.setattr(thumb_views, "resolve_thumb_blob", fake_resolve)
        resp = client.get(self._path("recaps/abc/a.jpg") + "?w=800")
        assert resp.status_code == 302
        assert seen["args"] == ("recaps/abc/a.jpg", 800)
        assert fake_signer[-1][0] == "thumbs/w800/recaps/abc/a.jpg.jpg"

    @override_settings(**ON)
    def test_signing_outage_is_503_not_public_fallback(self, client, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("iam down")

        monkeypatch.setattr(gcs, "generate_download_url", broken)
        assert client.get(self._path("recaps/abc/a.jpg")).status_code == 503

    @override_settings(**{**ON, "CORS_ALLOWED_ORIGINS": ["https://app.test"]})
    def test_cors_header_on_redirect(self, client, fake_signer):
        resp = client.get(self._path("recaps/abc/a.jpg"), HTTP_ORIGIN="https://app.test")
        # django-cors-headers covers /api/public/media/ (no CORS_URLS_REGEX);
        # "*" when the test env runs with DEBUG (CORS_ALLOW_ALL_ORIGINS).
        assert resp["Access-Control-Allow-Origin"] in ("https://app.test", "*")


@pytest.mark.django_db
class TestLegacyPathEndpointsWhenPrivate:
    @override_settings(**ON)
    @pytest.mark.parametrize("endpoint", ["/api/public/img/thumb", "/api/public/media/play"])
    def test_bare_private_key_refused(self, client, fake_signer, endpoint):
        for path in ("recaps/a.mov", "https://storage.googleapis.com/bkt/documents/x.mov"):
            assert client.get(endpoint, {"path": path}).status_code == 403
        assert fake_signer == []

    @override_settings(**ON)
    def test_play_accepts_media_url(self, client, fake_signer):
        resp = client.get("/api/public/media/play", {"path": media_url("recaps/a.mov")})
        assert resp.status_code == 302
        assert fake_signer[0][0] == "recaps/a.mov"
        assert fake_signer[0][2:] == ("video/mp4", "inline")

    @override_settings(**ON)
    def test_thumb_accepts_media_url_and_public_keys(self, client, fake_signer, monkeypatch):
        from utils import thumb_views

        monkeypatch.setattr(thumb_views, "resolve_thumb_blob", lambda b, w: f"thumbs/w{w}/{b}.jpg")
        resp = client.get("/api/public/img/thumb", {"path": media_url("recaps/a.jpg"), "w": 400})
        assert resp.status_code == 302
        assert fake_signer[-1][0] == "thumbs/w400/recaps/a.jpg.jpg"
        # Catalog thumbs stay on the direct public URL while it works.
        resp = client.get("/api/public/img/thumb", {"path": "products/p.jpg", "w": 400})
        assert resp["Location"] == "https://storage.googleapis.com/bkt/thumbs/w400/products/p.jpg.jpg"


# --------------------------------------------------------------------------
# Read-only audit command
# --------------------------------------------------------------------------
class TestAuditCommand:
    @override_settings(GS_BUCKET_NAME="bkt")
    def test_counts_per_prefix_and_never_writes(self, monkeypatch):
        from io import StringIO
        from types import SimpleNamespace

        from django.core.management import call_command

        listed = [
            ("products/a.jpg", 100),
            ("recaps/1700000000000-recap-1.jpg", 1000),  # guessable legacy name
            ("recaps/abc/0123456789abcdef0123456789abcdef.jpg", 2000),  # uuid4
            ("documents/id.pdf", 50),
        ]

        class FakeClient:
            def list_blobs(self, bucket, prefix=None, fields=None):
                assert bucket == "bkt"
                return [SimpleNamespace(name=n, size=s) for n, s in listed]

            def __getattr__(self, name):  # anything else = a write attempt
                raise AssertionError(f"audit must be read-only, called {name}")

        monkeypatch.setattr(gcs, "get_gcs_client", lambda: FakeClient())
        out = StringIO()
        call_command("private_media_audit", stdout=out)
        text = out.getvalue()
        assert "4 objects listed" in text
        lines = {l.split()[0]: l.split() for l in text.splitlines() if l[:1].isalpha() and "/" in l.split()[0]}
        assert lines["products/"][1] == "public"
        assert lines["recaps/abc/"][1] == "private" and lines["recaps/abc/"][4] == "0"
        assert lines["recaps/"][1] == "private" and lines["recaps/"][4] == "1"
        assert "documents/" in lines and lines["documents/"][4] == "1"

    def test_runbook(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("private_media_audit", "--runbook", stdout=out)
        text = out.getvalue()
        assert "remove-iam-policy-binding" in text and "ROLLBACK" in text
