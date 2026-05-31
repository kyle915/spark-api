"""Best-effort OCR for consumer receipts via Google Cloud Vision (REST).

Admin-triggered (the `runReceiptOcr` mutation). We POST to the Vision REST
`images:annotate` endpoint with DOCUMENT_TEXT_DETECTION using Application
Default Credentials — the same service-account auth GCS already uses on Cloud
Run, so there is NO extra Python dependency and no API key to manage.

Everything is wrapped so a missing dependency, a disabled Vision API, or a
parse failure degrades to "no fields extracted" rather than raising — the
admin can always fall back to reading the photo manually.

Enablement note: this needs the Cloud Vision API enabled on the GCP project
and the Cloud Run service account to have access (the cloud-platform scope is
already granted for GCS; the Vision API just needs to be turned on). Until
then, `run_ocr` returns ok=False with a human-readable reason.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.utils.dateparse import parse_date

logger = logging.getLogger(__name__)

_VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_MAX_TEXT = 8000


@dataclass
class OcrResult:
    ok: bool
    reason: str = ""
    text: str = ""
    store: str = ""
    amount: Decimal | None = None
    purchase_date: date | None = None


def _vision_text(image_bytes: bytes) -> str:
    """Call Vision REST and return the full recognized text (may raise)."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=_SCOPES)
    session = AuthorizedSession(creds)
    payload = {
        "requests": [
            {
                "image": {
                    "content": base64.b64encode(image_bytes).decode("ascii")
                },
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            }
        ]
    }
    resp = session.post(_VISION_ENDPOINT, json=payload, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    responses = data.get("responses") or [{}]
    first = responses[0] if responses else {}
    if first.get("error"):
        raise RuntimeError(
            str(first["error"].get("message") or first["error"])
        )
    fta = first.get("fullTextAnnotation") or {}
    return fta.get("text", "") or ""


# A money amount like 12.34 or $1,234.56 (two decimal places required).
_AMOUNT_RE = re.compile(r"\$?\s*(\d{1,6}(?:,\d{3})*[.,]\d{2})")
_TOTAL_HINT_RE = re.compile(
    r"(grand\s+total|total|amount\s+due|balance\s+due|amount\s+paid)", re.I
)
_DATE_RES = [
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b"),
]


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_amount(text: str) -> Decimal | None:
    """Most likely purchase total: prefer a "total" line, else the largest."""
    best_total: Decimal | None = None
    all_amounts: list[Decimal] = []
    for line in text.splitlines():
        line_has_total = bool(_TOTAL_HINT_RE.search(line))
        for m in _AMOUNT_RE.finditer(line):
            val = _to_decimal(m.group(1))
            if val is None:
                continue
            all_amounts.append(val)
            if line_has_total and (best_total is None or val > best_total):
                best_total = val
    if best_total is not None:
        return best_total
    return max(all_amounts) if all_amounts else None


def _parse_date(text: str) -> date | None:
    for rx in _DATE_RES:
        m = rx.search(text)
        if not m:
            continue
        raw = m.group(1)
        parsed = parse_date(raw)
        if parsed:
            return parsed
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


def _parse_store(text: str) -> str:
    """Heuristic: the merchant name is usually the first 'wordy' line."""
    for line in text.splitlines():
        s = line.strip()
        if len(s) >= 3 and any(c.isalpha() for c in s) and not _AMOUNT_RE.fullmatch(s):
            return s[:255]
    return ""


def run_ocr(image_bytes: bytes) -> OcrResult:
    """Run OCR + parse store / amount / date. Never raises."""
    if not image_bytes:
        return OcrResult(ok=False, reason="No image bytes.")
    try:
        text = _vision_text(image_bytes)
    except ImportError as exc:
        logger.warning("receipts.ocr: dependency unavailable: %s", exc)
        return OcrResult(ok=False, reason="OCR dependency unavailable.")
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the mutation
        logger.warning("receipts.ocr: Vision call failed: %s", exc)
        return OcrResult(
            ok=False,
            reason="OCR is unavailable (the Vision API may be disabled).",
        )
    if not text.strip():
        return OcrResult(ok=True, reason="No text recognized in the image.")
    return OcrResult(
        ok=True,
        text=text[:_MAX_TEXT],
        store=_parse_store(text),
        amount=_parse_amount(text),
        purchase_date=_parse_date(text),
    )
