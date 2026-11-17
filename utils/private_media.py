"""Private storage with signed reads.

Today the bucket is public-read (``allUsers:objectViewer``) and every
stored-object URL the API hands out is a plain
``https://storage.googleapis.com/<bucket>/<key>`` link: anyone holding a key
can fetch recap photos, receipts, BA documents, chat attachments.

This module lets the bucket be made private without breaking any reader.

* ``media_url(blob)`` is what every serving path calls (``utils.gcs.public_url``
  delegates here). With ``PRIVATE_MEDIA_ENABLED`` off it returns the old public
  URL, byte for byte. With it on, any key outside ``PUBLIC_PREFIXES`` becomes an
  opaque app URL::

      {PUBLIC_API_BASE_URL}/api/public/media/o/<token>/<original-filename>

  ``<token>`` is an HMAC over the blob key (and an expiry). Minting it is pure
  CPU (a few microseconds) — no GCS or IAM call per row, so list resolvers that
  serialize thousands of files cost the same as before. The original filename
  rides along in the path (unsigned, cosmetic) so every extension-based check
  on the web, mobile and server side (image vs video vs pdf, HEIC, zip names)
  keeps working.

* ``GET /api/public/media/o/<token>/<name>`` (``utils.media_views.media_object``)
  verifies the HMAC and 302s to a short-lived V4 signed GCS URL. Signing goes
  through the Cloud Run service account's IAM ``signBlob`` (no key file) and
  is cached per blob, so a page of 60 photos costs at most 60 signBlob calls the
  first time and none for the next ~45 minutes on that instance; the browser
  also caches the 302.

Two token lifetimes:

* **interactive** (default): expires at the end of the *next* 12h window, so it
  is valid for 12-24h and the URL is identical for every request inside a
  window (stable cache key for browsers and RN ``<Image>``).
* **durable** (``durable=True``): no expiry. For links written somewhere that is
  read later — client spreadsheets/CSVs, emailed links, printed exports.
  Revoked by retiring the signing key (see ``PRIVATE_MEDIA_TOKEN_KEYS``).

Public catalog assets (``PUBLIC_PREFIXES``) keep direct public URLs while
``PRIVATE_MEDIA_DIRECT_PUBLIC_PREFIXES`` is on. Turn that off before removing
``allUsers`` from the bucket; they then get durable tokens too.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from urllib.parse import quote, unquote, urlparse

from django.conf import settings

MEDIA_PATH_PREFIX = "/api/public/media/o/"

# Brand-facing catalog assets: shown on public pages, in emails, on the BA
# check-in screens. Everything NOT listed here is private by default (fail
# closed): recaps/, recap_files/, documents/, ba/, users/, chat_attachments/,
# connecteam-imports/, consumer-receipts/, campaign-reports/, exports/,
# custom-recaps/, uploads/, thumbs/ of private objects, and any new prefix.
PUBLIC_PREFIXES: tuple[str, ...] = (
    "products/",
    "tenants/images/",
    "receipt-campaign-images/",
)

_THUMB_PREFIX = "thumbs/"
_WINDOW_SECONDS_DEFAULT = 12 * 3600
_SIG_BYTES = 16  # 128-bit truncated HMAC-SHA256


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
def enabled() -> bool:
    return bool(getattr(settings, "PRIVATE_MEDIA_ENABLED", False))


def _direct_public_prefixes() -> bool:
    return bool(getattr(settings, "PRIVATE_MEDIA_DIRECT_PUBLIC_PREFIXES", True))


def _window_seconds() -> int:
    hours = int(getattr(settings, "PRIVATE_MEDIA_URL_WINDOW_HOURS", 12) or 12)
    return max(1, hours) * 3600


def _api_base() -> str:
    base = getattr(settings, "PRIVATE_MEDIA_BASE_URL", "") or getattr(
        settings, "PUBLIC_API_BASE_URL", ""
    )
    return (base or "").rstrip("/")


def _keyring() -> list[tuple[str, bytes]]:
    """``[(kid, key), ...]``; the first entry signs, every entry verifies.

    ``PRIVATE_MEDIA_TOKEN_KEYS="k2:<secret>,k1:<secret>"``. Rotation: prepend a
    new key (old links keep working), later drop the old one (every link it
    signed — including durable ones — stops working). When unset, a key is
    derived from SECRET_KEY (kid ``s0``); rotating SECRET_KEY then revokes all
    media links along with sessions.
    """
    raw = getattr(settings, "PRIVATE_MEDIA_TOKEN_KEYS", "") or ""
    ring: list[tuple[str, bytes]] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        kid, secret = part.split(":", 1)
        kid = kid.strip()
        secret = secret.strip()
        if kid and secret and "." not in kid and "/" not in kid:
            ring.append((kid, secret.encode()))
    if not ring:
        derived = hmac.new(
            str(settings.SECRET_KEY).encode(),
            b"spark-private-media-v1",
            hashlib.sha256,
        ).digest()
        ring.append(("s0", derived))
    return ring


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def clean_blob(blob: str | None) -> str | None:
    """Plain relative object key, or None for anything unsafe."""
    if not blob:
        return None
    key = str(blob).strip().lstrip("/")
    if not key or "\\" in key or "\x00" in key or "://" in key:
        return None
    if any(seg in ("", ".", "..") for seg in key.split("/")):
        return None
    return key


def is_public_blob(blob: str | None) -> bool:
    """True for catalog assets (and thumbs OF catalog assets)."""
    key = clean_blob(blob)
    if not key:
        return False
    if key.startswith(_THUMB_PREFIX):
        # thumbs/w400/<original>.jpg — public only if the original is.
        parts = key.split("/", 2)
        if len(parts) < 3:
            return False
        key = parts[2]
    return key.startswith(PUBLIC_PREFIXES)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(key: bytes, kid: str, payload: str) -> str:
    mac = hmac.new(key, f"{kid}.{payload}".encode(), hashlib.sha256).digest()
    return _b64e(mac[:_SIG_BYTES])


def window_expiry(now: float | None = None) -> int:
    """End of the NEXT window: valid 1-2 windows, constant within a window."""
    now = time.time() if now is None else now
    win = _window_seconds()
    return (int(now) // win + 2) * win


def mint_token(blob: str, *, durable: bool = False, now: float | None = None) -> str:
    key_blob = clean_blob(blob)
    if not key_blob:
        raise ValueError("invalid blob")
    exp = 0 if durable else window_expiry(now)
    payload = _b64e(
        json.dumps({"b": key_blob, "e": exp}, separators=(",", ":")).encode()
    )
    kid, key = _keyring()[0]
    return f"{kid}.{payload}.{_sign(key, kid, payload)}"


class TokenError(Exception):
    pass


class TokenExpired(TokenError):
    pass


def verify_token(
    token: str, *, check_expiry: bool = True, now: float | None = None
) -> str:
    """Return the blob key the token was minted for, or raise TokenError."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise TokenError("malformed")
    kid, payload, sig = parts
    key = dict(_keyring()).get(kid)
    if key is None:
        raise TokenError("unknown key")
    if not hmac.compare_digest(_sign(key, kid, payload), sig):
        raise TokenError("bad signature")
    try:
        data = json.loads(_b64d(payload))
        blob = clean_blob(data["b"])
        exp = int(data.get("e") or 0)
    except Exception as exc:  # noqa: BLE001
        raise TokenError("bad payload") from exc
    if not blob:
        raise TokenError("bad blob")
    if check_expiry and exp:
        now = time.time() if now is None else now
        if now > exp:
            raise TokenExpired("expired")
    return blob


def token_from_url(url: str | None) -> str | None:
    """The token segment of a ``/api/public/media/o/<token>[/name]`` URL."""
    if not url:
        return None
    path = urlparse(str(url)).path if "://" in str(url) else str(url)
    idx = path.find(MEDIA_PATH_PREFIX)
    if idx < 0:
        return None
    rest = path[idx + len(MEDIA_PATH_PREFIX):]
    token = unquote(rest.split("/", 1)[0])
    return token or None


def is_media_url(url: str | None) -> bool:
    return token_from_url(url) is not None


def blob_from_media_url(url: str | None, *, check_expiry: bool = False) -> str | None:
    """Decode a media URL back to its blob key (signature always checked).

    Used when a client echoes a URL it was given back into a write
    (e.g. mobile's ``updateBaProfile.eventPhotos``). Expiry is ignored by
    default there: the token proves the server once handed this caller that
    key; dropping a BA's photos because a screen sat open overnight is worse.
    """
    token = token_from_url(url)
    if not token:
        return None
    try:
        return verify_token(token, check_expiry=check_expiry)
    except TokenError:
        return None


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------
def _raw_public_url(key: str) -> str | None:
    bucket = getattr(settings, "GS_BUCKET_NAME", "")
    if not bucket:
        return None
    return f"https://storage.googleapis.com/{bucket}/{key}"


def media_url(blob: str | None, *, durable: bool = False) -> str | None:
    """The URL a reader should be handed for ``blob``.

    Flag off → the legacy public URL (unchanged behaviour). Flag on →
    a direct public URL for catalog prefixes (while
    PRIVATE_MEDIA_DIRECT_PUBLIC_PREFIXES), else an app media URL.
    Catalog assets that go through tokens always get durable ones.
    """
    if not blob:
        return None
    if not getattr(settings, "GS_BUCKET_NAME", ""):
        return None
    if not enabled():
        return _raw_public_url(str(blob).lstrip("/"))
    key = clean_blob(blob)
    if not key:
        return None
    public = is_public_blob(key)
    if public and _direct_public_prefixes():
        return _raw_public_url(key)
    token = mint_token(key, durable=durable or public)
    name = quote(key.rsplit("/", 1)[-1], safe="")
    base = _api_base()
    return f"{base}{MEDIA_PATH_PREFIX}{token}/{name}"


# ---------------------------------------------------------------------------
# Signed reads (the only network part — done in the redirect view, never in a
# list resolver)
# ---------------------------------------------------------------------------
_SIGNED_CACHE: dict[tuple, tuple[str, float]] = {}
_SIGNED_LOCK = threading.Lock()
_SIGNED_CACHE_MAX = 20000


def signed_ttl_seconds() -> int:
    minutes = int(getattr(settings, "PRIVATE_MEDIA_SIGNED_TTL_MINUTES", 60) or 60)
    return max(5, min(minutes, 7 * 24 * 60)) * 60


def _reuse_floor_seconds() -> int:
    # Hand out a cached signature only while it still has this long to live.
    return min(15 * 60, signed_ttl_seconds() // 2)


def signed_read_url(
    blob: str,
    *,
    response_type: str | None = None,
    response_disposition: str | None = None,
) -> tuple[str, float]:
    """``(url, expires_at_epoch)`` for a GET of ``blob``. Cached per process."""
    from utils.gcs import generate_download_url

    cache_key = (blob, response_type or "", response_disposition or "")
    now = time.time()
    with _SIGNED_LOCK:
        hit = _SIGNED_CACHE.get(cache_key)
        if hit and hit[1] - now > _reuse_floor_seconds():
            return hit
    ttl = signed_ttl_seconds()
    url = generate_download_url(
        blob,
        expiration_minutes=ttl // 60,
        response_type=response_type,
        response_disposition=response_disposition,
    )
    entry = (url, now + ttl)
    with _SIGNED_LOCK:
        if len(_SIGNED_CACHE) >= _SIGNED_CACHE_MAX:
            _SIGNED_CACHE.clear()
        _SIGNED_CACHE[cache_key] = entry
    return entry


def clear_signed_cache() -> None:
    with _SIGNED_LOCK:
        _SIGNED_CACHE.clear()


def serve_url(
    blob: str,
    *,
    response_type: str | None = None,
    response_disposition: str | None = None,
) -> tuple[str, int | None]:
    """URL to redirect a reader to for ``blob`` + max cache seconds for the 302.

    Direct public URL when that still works (flag off, or a catalog prefix
    while direct public prefixes are on) and no response overrides are
    needed; otherwise a signed URL. ``None`` cache seconds = caller's default.
    """
    if not response_type and not response_disposition:
        if not enabled() or (is_public_blob(blob) and _direct_public_prefixes()):
            return _raw_public_url(blob) or "", None
    url, expires_at = signed_read_url(
        blob,
        response_type=response_type,
        response_disposition=response_disposition,
    )
    remaining = int(expires_at - time.time()) - 5 * 60
    return url, max(0, min(remaining, 30 * 60))


__all__ = [
    "MEDIA_PATH_PREFIX",
    "PUBLIC_PREFIXES",
    "TokenError",
    "TokenExpired",
    "blob_from_media_url",
    "clean_blob",
    "clear_signed_cache",
    "enabled",
    "is_media_url",
    "is_public_blob",
    "media_url",
    "mint_token",
    "serve_url",
    "signed_read_url",
    "token_from_url",
    "verify_token",
    "window_expiry",
]
