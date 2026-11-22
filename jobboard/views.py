"""Public job board endpoints. The board token in the URL is the only auth.

GET  /api/public/jobs/<token>                  brand + open gigs (no applicant data)
POST /api/public/jobs/<token>/apply            book one or more gigs
GET  /api/public/jobs/release/<release_token>  inspect signed shift release window
POST /api/public/jobs/release/<release_token>  >36h self-release or <=36h urgent escalation
"""

from __future__ import annotations

import json
import logging

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from events.checkin_views import _client_ip, _err, _over_limit
from jobboard.gigs import board_for_token, public_board_payload
from jobboard.release import (
    ReleaseError,
    execute_shift_release,
    inspect_shift_release,
    resolve_release_target,
)
from jobboard.services import ApplyError, apply_to_gigs, clean_applicant

logger = logging.getLogger(__name__)

_NOT_FOUND = "This job board link isn't active. Ask Ignite for a current one."


@csrf_exempt
@require_http_methods(["GET"])
def public_job_board(request: HttpRequest, token: str) -> HttpResponse:
    if _over_limit("jobboard:view", _client_ip(request), limit=120, window=60):
        return _err("Too many requests. Try again in a minute.", status=429, code="rate_limited")
    found = board_for_token(token)
    if found is None:
        return _err(_NOT_FOUND, status=404, code="not_found")
    board, config = found
    return JsonResponse(public_board_payload(board, config))


@csrf_exempt
@require_http_methods(["POST"])
def public_job_board_apply(request: HttpRequest, token: str) -> HttpResponse:
    ip = _client_ip(request)
    if _over_limit("jobboard:apply:ip", ip, limit=10, window=600):
        return _err("Too many tries. Wait a few minutes and try again.", status=429, code="rate_limited")
    found = board_for_token(token)
    if found is None:
        return _err(_NOT_FOUND, status=404, code="not_found")
    board, config = found
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return _err("Something went wrong. Refresh and try again.")
    if not isinstance(data, dict):
        return _err("Something went wrong. Refresh and try again.")

    # Bots fill every field; people never see this one.
    if str(data.get("website") or "").strip():
        return JsonResponse({"results": []})

    try:
        applicant = clean_applicant(data)
    except ApplyError as exc:
        return _err(exc.message, code=exc.code)
    if _over_limit("jobboard:apply:phone", applicant.phone, limit=6, window=3600):
        return _err("Too many tries for this number. Try again later.", status=429, code="rate_limited")

    gig_ids = data.get("gigIds")
    if not isinstance(gig_ids, list):
        return _err("Pick at least one gig.", code="no_gigs")
    try:
        results = apply_to_gigs(board, config, applicant, gig_ids)
    except ApplyError as exc:
        return _err(exc.message, code=exc.code)
    logger.info(
        "job board apply board=%s gigs=%s booked=%s",
        board.id,
        len(results),
        sum(1 for r in results if r["status"] == "booked"),
    )
    return JsonResponse({"results": results, "email": applicant.email})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def public_shift_release(request: HttpRequest, release_token: str) -> HttpResponse:
    ip = _client_ip(request)
    if request.method == "GET":
        if _over_limit("jobboard:release:view", ip, limit=60, window=300):
            return _err("Too many requests. Try again in a minute.", status=429, code="rate_limited")
        target = resolve_release_target(release_token)
        if target is None:
            return _err(
                "This shift link is invalid or has expired.",
                status=404,
                code="invalid_token",
            )
        return JsonResponse(inspect_shift_release(target))

    if _over_limit("jobboard:release:submit", ip, limit=15, window=600):
        return _err("Too many tries. Wait a few minutes and try again.", status=429, code="rate_limited")
    try:
        data = json.loads((request.body or b"").decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return _err("Something went wrong. Refresh and try again.")
    if not isinstance(data, dict):
        return _err("Something went wrong. Refresh and try again.")

    try:
        payload = execute_shift_release(release_token, reason=str(data.get("reason") or ""))
    except ReleaseError as exc:
        return _err(exc.message, status=exc.status, code=exc.code)
    return JsonResponse(payload)
