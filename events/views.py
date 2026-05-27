"""Public (no-JWT) HTTP endpoints for the events app.

Right now this only exposes the *approval* path: clients receive a one-click
"Review & approve" email and need to land on a page they can actually
interact with — even though they may not be logged into Spark, may not have
a Spark account at all, or may have a Spark account scoped to a different
tenant. The previous version pointed the email button at
`/approvals?request=<id>`, which routes through `AdminOnly` and a
JWT-protected GraphQL query; for any client whose account didn't survive
the admin guard, the page blanked out (an uncaught useLazyLoadQuery error
behind a Suspense boundary with no ErrorBoundary).

These views fix that by issuing the *recipient* a short-lived signed token
in their email link, then exposing three operations against that token:

* GET  /api/public/approval/<token>            → fetch request details
* POST /api/public/approval/<token>            → flip status; body decides

Tokens are produced by `django.core.signing.TimestampSigner` (HMAC-SHA1
over a payload + a timestamp, with the project's SECRET_KEY). We pin the
salt so a stolen token from another flow (magic-link sign-in, password
reset, etc.) can't be replayed here. Default lifetime is 14 days, which is
slightly longer than the typical "approve this week's batch" cadence so
emails don't expire mid-Slack-thread.

Approve/decline reuse the same side effects as the GraphQL mutations
(`RequestActivityLog` row, `Event` materialization, requestor email, push
nudge) so a client clicking from email and an admin clicking in-app
produce identical downstream state. The compound payload also includes
the recipient's email so the activity log can attribute the action to a
specific person even when no User row exists for them.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from asgiref.sync import async_to_sync
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from events import models
from tenants.models import Tenant

logger = logging.getLogger(__name__)

# Salt scopes the signer to this specific flow. Don't reuse this string
# anywhere else — that's how Django keeps stolen tokens from other features
# (password reset, magic link) from being replayed against this endpoint.
_APPROVAL_TOKEN_SALT = "events.public_approval.v1"

# 14 days. Approval emails routinely sit in Slack/inbox queues for a few
# days; two weeks is enough margin without leaving compromised links live
# forever.
_APPROVAL_TOKEN_MAX_AGE_SECONDS = 14 * 24 * 60 * 60


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_APPROVAL_TOKEN_SALT)


def make_approval_token(request_id: int, recipient_email: str) -> str:
    """Issue a signed token for a recipient's approval email link.

    The payload binds request_id + recipient_email so the activity log
    can attribute the action even when no User row exists for the
    recipient. Verification re-splits on the first `:` so the local-part
    of the email is preserved verbatim even if it contains another `:`.
    """
    payload = f"{int(request_id)}:{(recipient_email or '').strip().lower()}"
    return _signer().sign(payload)


def _verify_approval_token(token: str) -> tuple[int, str]:
    """Verify + parse a token; raise BadSignature if invalid/expired."""
    payload = _signer().unsign(token, max_age=_APPROVAL_TOKEN_MAX_AGE_SECONDS)
    request_id_str, _, recipient_email = payload.partition(":")
    return int(request_id_str), recipient_email


def _serialize_request_for_public(req: models.Request) -> dict[str, Any]:
    """Render the subset of request fields the public approval page needs.

    Avoid leaking anything the recipient wouldn't already see in their
    own approval email (no internal notes, no ambassador rosters, no
    recap data — the page is just "here's what landed, approve or
    decline").
    """
    status_slug = ""
    status_name = ""
    if getattr(req, "status_id", None):
        try:
            status_obj = models.RequestStatus.objects.filter(id=req.status_id).first()
            status_slug = (getattr(status_obj, "slug", "") or "").lower()
            status_name = getattr(status_obj, "name", "") or ""
        except Exception:
            pass

    retailer_name = ""
    if getattr(req, "retailer", None):
        retailer_name = getattr(req.retailer, "name", "") or ""

    tenant_name = ""
    if getattr(req, "tenant_id", None):
        try:
            tenant_name = Tenant.objects.values_list("name", flat=True).get(
                id=req.tenant_id
            )
        except Tenant.DoesNotExist:
            pass

    return {
        "id": req.id,
        "displayId": f"REQ-{req.id}",
        "tenantName": tenant_name,
        "accountName": retailer_name or getattr(req, "name", "") or "",
        "address": getattr(req, "address", "") or "",
        "date": str(getattr(req, "date", "")) if getattr(req, "date", None) else "",
        "startTime": (
            str(getattr(req, "start_time", "")) if getattr(req, "start_time", None) else ""
        ),
        "endTime": (
            str(getattr(req, "end_time", "")) if getattr(req, "end_time", None) else ""
        ),
        "activationType": (
            getattr(getattr(req, "request_type", None), "name", "") or ""
        ),
        "distributor": getattr(getattr(req, "distributor", None), "name", "") or "",
        "requestorName": getattr(req, "client_name", "") or "",
        "requestorEmail": (
            getattr(req, "requestor_email", "")
            or getattr(req, "client_email", "")
            or ""
        ),
        "status": {"slug": status_slug, "name": status_name},
        "declineReason": getattr(req, "decline_reason", "") or "",
    }


def _resolve_status(slug: str, tenant_id: int) -> models.RequestStatus | None:
    return models.RequestStatus.objects.get_by_slug(slug=slug, tenant=tenant_id)


def _log_public_action(
    request: models.Request,
    *,
    actor_email: str,
    summary: str,
    metadata: dict[str, Any],
) -> None:
    """Best-effort audit log; never blocks the action if it fails."""
    try:
        models.RequestActivityLog.objects.create(
            tenant=request.tenant,
            request=request,
            kind=models.RequestActivityLog.KIND_STATUS_CHANGED,
            actor_user=None,
            summary=summary,
            metadata={**metadata, "actor_email": actor_email, "source": "public-link"},
        )
    except Exception:
        logger.exception(
            "public_approval: activity log write failed for request_id=%s", request.id
        )


def _do_approve(request_obj: models.Request, recipient_email: str) -> None:
    """Apply approval side effects mirroring the GraphQL mutation path."""
    # Late imports so cron-only / management-only commands don't pay the
    # cost of pulling in mailer + notification helpers at process start.
    from events.mutations import (
        _notify_notification_group_users_for_request,
        _notify_requestor_for_request_approved,
        _push_requestor_for_request_verdict,
        _resolve_request_location,
    )

    approved = _resolve_status("approved", request_obj.tenant_id)
    if not approved:
        raise RuntimeError("Approval status not configured for this tenant.")

    prev_status_slug = ""
    try:
        if request_obj.status_id:
            prev = models.RequestStatus.objects.filter(id=request_obj.status_id).first()
            prev_status_slug = (getattr(prev, "slug", "") or "").lower()
    except Exception:
        prev_status_slug = ""

    # Attribute the approval to the person the token was issued to (the RMM
    # who clicked the link in their email). approved_by is a User FK, so it
    # only sticks when that email maps to a Spark user — which the RMMs do.
    # The approved-email mailer falls back to showing the raw email otherwise.
    from tenants.models import User as _User

    approver = (
        _User.objects.filter(email__iexact=recipient_email).first()
        if recipient_email
        else None
    )

    request_obj.status = approved
    if approver:
        request_obj.approved_by = approver
    request_obj.save()

    _log_public_action(
        request_obj,
        actor_email=recipient_email,
        summary=f"Status: {prev_status_slug or '—'} → approved",
        metadata={"from": prev_status_slug, "to": "approved"},
    )

    # Idempotent Event materialization — copy the same shape the GraphQL
    # path uses so an Event exists for ops staffing as soon as the client
    # clicks the email.
    existing = (
        models.Event.objects.filter(request_id=request_obj.id).order_by("-id").first()
    )
    if existing is None:
        try:
            # created_by is NOT NULL on Event — passing None raised
            # IntegrityError (swallowed below), so token-approved requests
            # never got an event (no Assign-BA / Post-to-board / job). Use
            # the approver, else the assigned RMM, else the request creator.
            event_creator = (
                approver or request_obj.rmm_asigned or request_obj.created_by
            )
            async_to_sync(models.Event.objects.from_request)(
                request=request_obj,
                created_by=event_creator,
            )
            # Event exists now → create its Pending Job. The Request
            # post_save signal fired before the event existed, so do it
            # explicitly here. Idempotent.
            from events.signals import create_pending_jobs_for_request

            create_pending_jobs_for_request(request_obj)
        except Exception:
            logger.exception(
                "public_approval: Event.from_request failed for request_id=%s",
                request_obj.id,
            )

    # Notifications use the async helpers defined in events/mutations.py;
    # we step into sync land via async_to_sync so this view stays a plain
    # Django view (no ASGI handler needed).
    try:
        location = async_to_sync(_resolve_request_location)(request_obj)
        async_to_sync(_notify_notification_group_users_for_request)(
            request_obj, location
        )
        async_to_sync(_notify_requestor_for_request_approved)(
            request_obj, location, approver_email_fallback=recipient_email
        )
        async_to_sync(_push_requestor_for_request_verdict)(request_obj, approved=True)
    except Exception:
        logger.exception(
            "public_approval: post-approve notifications failed for request_id=%s",
            request_obj.id,
        )


def _do_decline(
    request_obj: models.Request, recipient_email: str, reason: str
) -> None:
    """Apply decline side effects mirroring the GraphQL mutation path."""
    from events.mutations import (
        _notify_requestor_for_request_declined,
        _push_requestor_for_request_verdict,
        _resolve_request_location,
    )

    declined = _resolve_status("declined", request_obj.tenant_id)
    if not declined:
        raise RuntimeError("Decline status not configured for this tenant.")

    prev_status_slug = ""
    try:
        if request_obj.status_id:
            prev = models.RequestStatus.objects.filter(id=request_obj.status_id).first()
            prev_status_slug = (getattr(prev, "slug", "") or "").lower()
    except Exception:
        prev_status_slug = ""

    request_obj.status = declined
    request_obj.decline_reason = reason or ""
    request_obj.save()

    _log_public_action(
        request_obj,
        actor_email=recipient_email,
        summary=f"Status: {prev_status_slug or '—'} → declined",
        metadata={
            "from": prev_status_slug,
            "to": "declined",
            "decline_reason": (reason or "")[:500],
        },
    )

    try:
        location = async_to_sync(_resolve_request_location)(request_obj)
        async_to_sync(_notify_requestor_for_request_declined)(
            request=request_obj,
            location=location,
            reviewed_by_name=recipient_email or "client",
            reviewed_by_email=recipient_email or "",
        )
        async_to_sync(_push_requestor_for_request_verdict)(
            request_obj,
            approved=False,
            decline_reason=reason,
        )
    except Exception:
        logger.exception(
            "public_approval: post-decline notifications failed for request_id=%s",
            request_obj.id,
        )


def _verify_or_4xx(token: str) -> tuple[int, str] | HttpResponse:
    """Helper: return (request_id, recipient_email) or a 4xx response."""
    try:
        return _verify_approval_token(token)
    except SignatureExpired:
        return JsonResponse(
            {
                "error": "expired",
                "message": "This approval link has expired. Please ask the requestor to resend.",
            },
            status=410,
        )
    except BadSignature:
        return JsonResponse(
            {
                "error": "invalid",
                "message": "This approval link is invalid or has been tampered with.",
            },
            status=400,
        )


def _load_request_or_404(request_id: int) -> models.Request | HttpResponse:
    try:
        return models.Request.objects.select_related(
            "tenant",
            "timezone",
            "retailer__location__state",
            "distributor__location__state",
            "request_type",
        ).get(id=request_id)
    except models.Request.DoesNotExist:
        return JsonResponse(
            {"error": "not_found", "message": "Request not found."}, status=404
        )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def public_approval_view(request: HttpRequest, token: str) -> HttpResponse:
    """Single endpoint for the public approval flow.

    GET                          → returns request details for the page
    POST {action: "approve"}     → flips status to approved
    POST {action: "decline", reason}
                                 → flips status to declined

    Combining all three under one path keeps the URL surface (and the
    email-link template) small. The action verb lives in the JSON body
    instead of the path because the email button itself is GET-only —
    the page-side fetch upgrades to POST once the user clicks.
    """
    verified = _verify_or_4xx(token)
    if isinstance(verified, HttpResponse):
        return verified
    request_id, recipient_email = verified

    loaded = _load_request_or_404(request_id)
    if isinstance(loaded, HttpResponse):
        return loaded
    req: models.Request = loaded

    if request.method == "GET":
        return JsonResponse(
            {
                "request": _serialize_request_for_public(req),
                "recipientEmail": recipient_email,
            }
        )

    # POST path
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        body = {}
    action = (body.get("action") or "").strip().lower()
    reason = (body.get("reason") or "").strip()

    # Idempotency: if the request was already moved, return its current
    # state instead of erroring. The client UI uses this to render a
    # "Already approved by ..." message gracefully on re-clicks.
    current_status = (
        models.RequestStatus.objects.filter(id=req.status_id)
        .values_list("slug", flat=True)
        .first()
        if req.status_id
        else None
    )
    if current_status in {"approved", "declined"}:
        return JsonResponse(
            {
                "request": _serialize_request_for_public(req),
                "recipientEmail": recipient_email,
                "alreadyResolved": True,
            },
            status=200,
        )

    if action == "approve":
        try:
            _do_approve(req, recipient_email)
        except Exception as exc:
            logger.exception(
                "public_approval: approve failed for request_id=%s", req.id
            )
            return JsonResponse(
                {"error": "approve_failed", "message": str(exc)}, status=500
            )
        req.refresh_from_db()
        return JsonResponse(
            {
                "request": _serialize_request_for_public(req),
                "recipientEmail": recipient_email,
                "result": "approved",
            }
        )

    if action == "decline":
        if not reason:
            return JsonResponse(
                {
                    "error": "reason_required",
                    "message": "Please share a quick reason so the requestor knows what to change.",
                },
                status=400,
            )
        try:
            _do_decline(req, recipient_email, reason)
        except Exception as exc:
            logger.exception(
                "public_approval: decline failed for request_id=%s", req.id
            )
            return JsonResponse(
                {"error": "decline_failed", "message": str(exc)}, status=500
            )
        req.refresh_from_db()
        return JsonResponse(
            {
                "request": _serialize_request_for_public(req),
                "recipientEmail": recipient_email,
                "result": "declined",
            }
        )

    return JsonResponse(
        {"error": "unknown_action", "message": f"Unknown action: {action!r}"},
        status=400,
    )
