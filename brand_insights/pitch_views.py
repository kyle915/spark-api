"""Public (no-JWT) Buyer Pitch Deck leave-behind.

Mounted under ``/api/public/``:

* ``GET  /api/public/pitch/<token>/`` — unlocked payload, or locked preview
  when a passcode is required and none was supplied.
* ``POST /api/public/pitch/<token>/`` — unlock with ``{"passcode": "…"}``.

Security:
- Token is the capability. Revoked → 403, expired → 410, missing → 404.
- Passcodes use Django ``check_password`` (constant-time).
- Cost fields are stripped unless the deck opted into ``include_costs``.
- No ambassador, agency, or owner PII in the public payload.
"""

from __future__ import annotations

import json
import logging

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from brand_insights.pitch_decks import (
    locked_pitch_deck_preview,
    resolve_public_pitch_deck,
    unlocked_pitch_deck_payload,
    verify_passcode,
)

logger = logging.getLogger(__name__)


def _json_error(code: str, message: str, status: int) -> JsonResponse:
    resp = JsonResponse({"error": code, "message": message}, status=status)
    resp["Cache-Control"] = "no-store"
    resp["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _json_ok(payload: dict, *, locked: bool = False) -> JsonResponse:
    body = {"ok": True, "locked": locked, **payload}
    resp = JsonResponse(body)
    resp["Cache-Control"] = "private, no-store"
    resp["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def _read_passcode(request: HttpRequest) -> str:
    raw = (request.headers.get("X-Pitch-Passcode") or "").strip()
    if raw:
        return raw
    raw = (request.GET.get("passcode") or "").strip()
    if raw:
        return raw
    if request.method == "POST" and request.body:
        try:
            data = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if isinstance(data, dict):
            return str(data.get("passcode") or "").strip()
    return ""


@csrf_exempt
@require_http_methods(["GET", "POST"])
def public_pitch_deck_view(request: HttpRequest, token: str) -> JsonResponse:
    deck, reason, status = resolve_public_pitch_deck(token)
    if deck is None:
        if reason == "revoked":
            return _json_error(
                "revoked",
                "This Buyer Pitch Deck link has been revoked.",
                status,
            )
        if reason == "expired":
            return _json_error(
                "expired",
                "This Buyer Pitch Deck link has expired.",
                status,
            )
        return _json_error(
            "not_found",
            "Buyer Pitch Deck not found.",
            status,
        )

    try:
        if deck.has_passcode:
            supplied = _read_passcode(request)
            if not supplied:
                return _json_ok(locked_pitch_deck_preview(deck), locked=True)
            if not verify_passcode(supplied, deck.passcode_hash):
                return _json_error(
                    "passcode_required",
                    "Incorrect passcode.",
                    403,
                )
        payload = unlocked_pitch_deck_payload(deck)
        return _json_ok(payload, locked=False)
    except Exception:  # noqa: BLE001
        logger.exception("public pitch deck failed token=%s", token[:12])
        return _json_error(
            "server_error",
            "Could not load this Buyer Pitch Deck right now.",
            500,
        )
