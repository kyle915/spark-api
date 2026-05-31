"""Public (no-JWT) HTTP endpoints for consumer receipt upload.

A shopper at a sampling event scans a QR / opens a link tied to that event,
sees what they're uploading a receipt *for*, and submits a photo of their
purchase receipt (plus optional self-reported details). No login, no Spark
account — exactly like the public *approval* flow in `events/views.py`,
which these views are modeled on.

Two operations, both keyed off a per-event signed token (see
`receipts/tokens.py`):

* GET  /api/public/receipts/<token>            → event/brand display info
* POST /api/public/receipts/<token>/submit     → store image + create receipt

The consumer can't call the authenticated `getUploadUrl` GraphQL field
(that needs a JWT), so the image is stored server-side here: the POST body
carries the bytes (multipart file OR base64 JSON), we validate type/size,
push to GCS via `utils.gcs.upload_bytes` under
`consumer-receipts/<tenant>/<event>/...`, and create a `pending`
ConsumerReceipt. The admin queue later resolves the stored blob path to a
public URL.

Both views are CSRF-exempt (token-authenticated, cross-origin from the
public upload page) — same as `public_approval_view`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from asgiref.sync import sync_to_async
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from events.models import Event
from receipts import models
from receipts.tokens import (
    BadSignature,
    SignatureExpired,
    verify_event_receipt_token,
)
from tenants.models import Tenant
from utils.gcs import public_url, upload_bytes

logger = logging.getLogger(__name__)

# Accepted receipt image MIME types. Kept tight on purpose — a receipt is a
# photo or a scan, so we allow the common camera/scan formats plus PDF (some
# stores email a PDF receipt). HEIC included because iPhone cameras default
# to it. Anything else is rejected before it ever touches GCS.
_ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    "application/pdf",
}

# Map content type → file extension for the stored blob name.
_EXTENSION_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "image/heif": "heif",
    "application/pdf": "pdf",
}

# Hard ceiling on a single receipt upload. The project allows 100MB uploads
# globally (DATA_UPLOAD_MAX_MEMORY_SIZE), but a phone photo of a receipt is
# ~a few MB; 15MB is comfortably above a high-res scan while keeping an
# abusive payload off the bucket.
_MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _verify_or_4xx(token: str) -> int | HttpResponse:
    """Return the event id for a valid token, or a 4xx JsonResponse."""
    try:
        return verify_event_receipt_token(token)
    except SignatureExpired:
        return JsonResponse(
            {
                "error": "expired",
                "message": "This upload link has expired. Please ask the event team for a new one.",
            },
            status=410,
        )
    except (BadSignature, ValueError):
        return JsonResponse(
            {
                "error": "invalid",
                "message": "This upload link is invalid or has been tampered with.",
            },
            status=400,
        )


def _load_event_or_404(event_id: int) -> Event | HttpResponse:
    try:
        return Event.objects.select_related("tenant").get(id=event_id)
    except Event.DoesNotExist:
        return JsonResponse(
            {"error": "not_found", "message": "Event not found."}, status=404
        )


def _event_display_payload(event: Event) -> dict[str, Any]:
    """The subset of event/brand info the public upload page renders.

    Intentionally minimal — no internal notes, no roster, no recap data.
    Just enough for the shopper to know what they're uploading a receipt
    for: the event name, the brand (tenant) name, and any product context.
    """
    tenant_name = ""
    if getattr(event, "tenant_id", None):
        tenant_name = getattr(event.tenant, "name", "") or ""

    return {
        "eventName": getattr(event, "name", "") or "",
        "brandName": tenant_name,
        "product": getattr(event, "notes", "") or "",
        "address": getattr(event, "address", "") or "",
    }


@csrf_exempt
@require_http_methods(["GET"])
def public_receipt_event_view(request: HttpRequest, token: str) -> HttpResponse:
    """GET — resolve a token to the event/brand display info for the page."""
    verified = _verify_or_4xx(token)
    if isinstance(verified, HttpResponse):
        return verified
    event_id = verified

    loaded = _load_event_or_404(event_id)
    if isinstance(loaded, HttpResponse):
        return loaded
    event: Event = loaded

    return JsonResponse({"event": _event_display_payload(event)})


def _extract_image(request: HttpRequest) -> tuple[bytes, str] | HttpResponse:
    """Pull (bytes, content_type) from a multipart OR base64-JSON body.

    Returns a 4xx JsonResponse on any validation failure (missing image,
    bad base64, disallowed type, too large).
    """
    content_type = (request.content_type or "").lower()

    raw: bytes | None = None
    declared_type: str = ""

    if content_type.startswith("multipart/form-data"):
        upload = request.FILES.get("image") or request.FILES.get("file")
        if upload is None:
            return JsonResponse(
                {"error": "image_required", "message": "No receipt image was attached."},
                status=400,
            )
        raw = upload.read()
        declared_type = (getattr(upload, "content_type", "") or "").lower()
    else:
        # JSON body with base64. Accept either a bare base64 string or a
        # data: URL (data:image/jpeg;base64,....).
        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        b64 = body.get("image") or body.get("imageBase64") or body.get("file")
        if not b64 or not isinstance(b64, str):
            return JsonResponse(
                {"error": "image_required", "message": "No receipt image was provided."},
                status=400,
            )
        declared_type = (body.get("contentType") or body.get("content_type") or "").lower()
        if b64.startswith("data:"):
            header, _, data_part = b64.partition(",")
            # data:image/jpeg;base64
            meta = header[5:].split(";")[0].strip().lower()
            if meta:
                declared_type = meta
            b64 = data_part
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return JsonResponse(
                {"error": "bad_image", "message": "Receipt image is not valid base64."},
                status=400,
            )

    if not raw:
        return JsonResponse(
            {"error": "image_required", "message": "Receipt image was empty."},
            status=400,
        )
    if len(raw) > _MAX_IMAGE_BYTES:
        return JsonResponse(
            {
                "error": "too_large",
                "message": "Receipt image is too large (max 15MB).",
            },
            status=413,
        )

    declared_type = declared_type or "image/jpeg"
    if declared_type not in _ALLOWED_CONTENT_TYPES:
        return JsonResponse(
            {
                "error": "unsupported_type",
                "message": f"Unsupported image type: {declared_type!r}.",
            },
            status=415,
        )

    return raw, declared_type


def _clean_str(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _build_receipt_fields(request: HttpRequest) -> dict[str, Any]:
    """Read the optional consumer/store/purchase fields from the request.

    Works for both multipart (request.POST) and JSON bodies; multipart
    fields take precedence, then JSON. Every field is optional.
    """
    data: dict[str, Any] = {}
    if request.content_type and request.content_type.lower().startswith(
        "multipart/form-data"
    ):
        data = {k: request.POST.get(k) for k in request.POST.keys()}
    else:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}

    def pick(*names: str) -> Any:
        for name in names:
            if name in data and data[name] not in (None, ""):
                return data[name]
        return None

    fields: dict[str, Any] = {
        "consumer_name": _clean_str(pick("consumerName", "consumer_name"), 255),
        "consumer_email": _clean_str(pick("consumerEmail", "consumer_email"), 254),
        "consumer_phone": _clean_str(pick("consumerPhone", "consumer_phone"), 50),
        "store_name": _clean_str(pick("storeName", "store_name"), 255),
        "product": _clean_str(pick("product"), 10000),
    }

    # Consumer payout details. payout_handle is the consumer's Venmo username
    # (a public identifier, NOT a credential); strip a leading "@".
    # payout_method defaults to "venmo". Harmless on the legacy event flow,
    # which simply never sends these.
    handle = _clean_str(pick("payoutHandle", "payout_handle"), 255)
    if handle:
        fields["payout_handle"] = handle.lstrip("@")
    method = _clean_str(pick("payoutMethod", "payout_method"), 16)
    fields["payout_method"] = method or "venmo"

    # purchase_date — accept an ISO date string; ignore anything unparseable
    # rather than 400 (the field is optional, best-effort).
    purchase_date = pick("purchaseDate", "purchase_date")
    if purchase_date:
        from django.utils.dateparse import parse_date

        parsed = parse_date(str(purchase_date)[:10])
        if parsed is not None:
            fields["purchase_date"] = parsed

    # amount — best-effort decimal parse; ignore if not a number.
    amount = pick("amount")
    if amount not in (None, ""):
        from decimal import Decimal, InvalidOperation

        try:
            fields["amount"] = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            pass

    return fields


@csrf_exempt
@require_http_methods(["POST"])
def public_receipt_submit_view(request: HttpRequest, token: str) -> HttpResponse:
    """POST — store the receipt image to GCS and create a pending receipt."""
    verified = _verify_or_4xx(token)
    if isinstance(verified, HttpResponse):
        return verified
    event_id = verified

    loaded = _load_event_or_404(event_id)
    if isinstance(loaded, HttpResponse):
        return loaded
    event: Event = loaded

    extracted = _extract_image(request)
    if isinstance(extracted, HttpResponse):
        return extracted
    image_bytes, content_type = extracted

    extension = _EXTENSION_BY_CONTENT_TYPE.get(content_type, "jpg")
    blob_name = (
        f"consumer-receipts/{event.tenant_id}/{event.id}/"
        f"{uuid.uuid4().hex}.{extension}"
    )

    try:
        upload_bytes(blob_name, image_bytes, content_type=content_type)
    except Exception:
        logger.exception(
            "public_receipt_submit: GCS upload failed for event_id=%s", event.id
        )
        return JsonResponse(
            {
                "error": "upload_failed",
                "message": "We couldn't store your receipt. Please try again.",
            },
            status=502,
        )

    fields = _build_receipt_fields(request)

    try:
        models.ConsumerReceipt.objects.create(
            tenant=event.tenant,
            event=event,
            image=blob_name,
            status=models.ConsumerReceipt.STATUS_PENDING,
            **fields,
        )
    except Exception:
        logger.exception(
            "public_receipt_submit: receipt row create failed for event_id=%s",
            event.id,
        )
        return JsonResponse(
            {
                "error": "create_failed",
                "message": "We couldn't record your receipt. Please try again.",
            },
            status=500,
        )

    return JsonResponse({"ok": True})


# ---------------------------------------------------------------------------
# Campaign public surface (GoToAisle-style). A campaign is addressed by its
# slug (no token — the link is meant to be public / QR'd / printed on signage)
# and is gated only by `is_active`. New consumer submissions attach to the
# campaign and capture the consumer's Venmo handle for later payout.
# ---------------------------------------------------------------------------
def _load_campaign_or_404(slug: str):
    """Return an ACTIVE ReceiptCampaign for the slug, or a 4xx JsonResponse."""
    try:
        campaign = models.ReceiptCampaign.objects.select_related("tenant").get(
            slug=slug
        )
    except models.ReceiptCampaign.DoesNotExist:
        return JsonResponse(
            {"error": "not_found", "message": "Campaign not found."},
            status=404,
        )
    if not campaign.is_active:
        # Don't leak that an inactive campaign exists — same shape as missing.
        return JsonResponse(
            {
                "error": "not_found",
                "message": "This campaign is not currently active.",
            },
            status=404,
        )
    return campaign


def _campaign_display_payload(campaign) -> dict[str, Any]:
    """The subset of campaign/brand info the public upload page renders."""
    reward = campaign.reward_amount or Decimal("0")
    brand = ""
    if getattr(campaign, "tenant_id", None):
        brand = getattr(campaign.tenant, "name", "") or ""
    return {
        "name": campaign.name or "",
        "brandName": brand,
        "headline": campaign.headline or "",
        "description": campaign.description or "",
        "product": campaign.product or "",
        "rewardAmount": f"{reward:.2f}",
        "rewardLabel": f"${reward:.2f}",
        "isActive": campaign.is_active,
    }


@csrf_exempt
@require_http_methods(["GET"])
def public_campaign_view(request: HttpRequest, slug: str) -> HttpResponse:
    """GET — resolve a campaign slug to its public display info."""
    loaded = _load_campaign_or_404(slug)
    if isinstance(loaded, HttpResponse):
        return loaded
    return JsonResponse({"campaign": _campaign_display_payload(loaded)})


@csrf_exempt
@require_http_methods(["POST"])
def public_campaign_submit_view(
    request: HttpRequest, slug: str
) -> HttpResponse:
    """POST — store the receipt image to GCS + create a pending receipt."""
    loaded = _load_campaign_or_404(slug)
    if isinstance(loaded, HttpResponse):
        return loaded
    campaign = loaded

    extracted = _extract_image(request)
    if isinstance(extracted, HttpResponse):
        return extracted
    image_bytes, content_type = extracted

    extension = _EXTENSION_BY_CONTENT_TYPE.get(content_type, "jpg")
    blob_name = (
        f"consumer-receipts/{campaign.tenant_id}/campaign-{campaign.id}/"
        f"{uuid.uuid4().hex}.{extension}"
    )

    try:
        upload_bytes(blob_name, image_bytes, content_type=content_type)
    except Exception:
        logger.exception(
            "public_campaign_submit: GCS upload failed for campaign_id=%s",
            campaign.id,
        )
        return JsonResponse(
            {
                "error": "upload_failed",
                "message": "We couldn't store your receipt. Please try again.",
            },
            status=502,
        )

    fields = _build_receipt_fields(request)

    try:
        models.ConsumerReceipt.objects.create(
            tenant=campaign.tenant,
            campaign=campaign,
            image=blob_name,
            status=models.ConsumerReceipt.STATUS_PENDING,
            **fields,
        )
    except Exception:
        logger.exception(
            "public_campaign_submit: receipt create failed for campaign_id=%s",
            campaign.id,
        )
        return JsonResponse(
            {
                "error": "create_failed",
                "message": "We couldn't record your receipt. Please try again.",
            },
            status=500,
        )

    return JsonResponse({"ok": True})


# Async wrappers — config/urls.py wires the public surface into an ASGI app
# alongside async GraphQL views. Django runs sync views in a threadpool
# under ASGI, but the events public views are plain sync; we keep these
# sync too for symmetry. (No async needed: the only I/O is GCS + a single
# ORM create, both fine in the sync threadpool.)
__all__ = [
    "public_receipt_event_view",
    "public_receipt_submit_view",
    "public_campaign_view",
    "public_campaign_submit_view",
]
