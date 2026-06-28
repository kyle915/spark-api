"""Shift-extension approve/decline workflow — shared service layer.

Used by BOTH the authenticated admin GraphQL mutation (resolve_shift_extension)
and the public one-click email link view (ambassadors.views.extension_approval_view),
so the decision logic + notifications live in exactly one place.

A ShiftExtensionRequest is created (status="pending") when a BA taps "Extend"
mid-shift (ambassadors.mutations.request_extension). The Ignite admin team gets
a push + email; this module is how that request gets approved/denied:
  - sets status + approved_minutes + resolved_at/by
  - notifies the BA of the decision (push)
The mobile app reads status / approved_minutes to extend the BA's activation
window once approved.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

logger = logging.getLogger(__name__)

# Unique salt so an extension token can never be replayed against another
# tokenized flow (request approval, receipts, etc. each use their own salt).
_EXTENSION_TOKEN_SALT = "ambassadors.extension_approval.v1"
_TOKEN_MAX_AGE = 30 * 24 * 60 * 60  # 30 days — a mid-shift ask is short-lived


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_EXTENSION_TOKEN_SALT)


def make_extension_token(extension_id: int) -> str:
    """Signed, expiring token embedding the extension's pk for the email link."""
    return _signer().sign(str(int(extension_id)))


def verify_extension_token(token: str) -> int | None:
    """Return the extension pk for a valid token, else None (bad/expired)."""
    try:
        raw = _signer().unsign(token, max_age=_TOKEN_MAX_AGE)
        return int(raw)
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None


def public_extension_url(token: str) -> str:
    """Absolute URL to the backend-rendered one-click approval page."""
    base = (getattr(settings, "PUBLIC_API_BASE_URL", "") or "").rstrip("/")
    return f"{base}/api/public/extension/{token}"


def user_is_ignite_admin(user) -> bool:
    """True for the Ignite admin team only (staff/superuser, role=spark-admin,
    or an @igniteproductions.co address — honoring the suppression exclude)."""
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    role = getattr(user, "role", None)
    if getattr(role, "slug", None) == "spark-admin":
        return True
    try:
        from utils.graphql.permissions import email_grants_ignite_admin

        return bool(email_grants_ignite_admin(getattr(user, "email", "") or ""))
    except Exception:  # noqa: BLE001
        return False


def resolve_extension(
    extension,
    *,
    approve: bool,
    approved_minutes: int | None = None,
    actor_user=None,
) -> dict:
    """Approve or deny a ShiftExtensionRequest. SYNC + idempotent.

    Returns a dict the callers turn into a GraphQL response / HTML page:
      {ok, already, status, approved_minutes, ba_name, venue, minutes_requested, message}

    On approve, approved_minutes defaults to the minutes the BA requested.
    Pushes the BA the decision (best-effort). Never raises on notify failure.
    """
    from django.utils import timezone as _tz
    from ambassadors.models import ShiftExtensionRequest

    ext = extension
    event = getattr(ext, "event", None)
    ba = getattr(ext, "ambassador", None)
    ba_user = getattr(ba, "user", None)
    ba_name = (
        f"{getattr(ba_user, 'first_name', '') or ''} "
        f"{getattr(ba_user, 'last_name', '') or ''}"
    ).strip() or "The BA"
    venue = getattr(event, "name", None) or "their shift"

    base = {
        "ba_name": ba_name,
        "venue": venue,
        "minutes_requested": ext.minutes_requested,
    }

    # Idempotent: if already resolved, report the existing decision.
    if ext.status != ShiftExtensionRequest.STATUS_PENDING:
        return {
            "ok": True,
            "already": True,
            "status": ext.status,
            "approved_minutes": ext.approved_minutes,
            "message": f"This request was already {ext.status}.",
            **base,
        }

    if approve:
        mins = approved_minutes if approved_minutes else ext.minutes_requested
        ext.status = ShiftExtensionRequest.STATUS_APPROVED
        ext.approved_minutes = int(mins) if mins else ext.minutes_requested
    else:
        ext.status = ShiftExtensionRequest.STATUS_DENIED
        ext.approved_minutes = None
    ext.resolved_at = _tz.now()
    if actor_user is not None and getattr(actor_user, "id", None):
        ext.resolved_by = actor_user
    ext.save(
        update_fields=[
            "status", "approved_minutes", "resolved_at", "resolved_by",
            "updated_at",
        ]
    )

    # Tell the BA. Best-effort — never fail the decision on a push hiccup.
    try:
        from ambassadors.push import _send_push_to_user_sync

        if ba_user and getattr(ba_user, "id", None):
            if approve:
                title = "✅ Extension approved"
                body = (
                    f"You're cleared for +{ext.approved_minutes} min at {venue}."
                )
            else:
                title = "Extension not approved"
                body = (
                    f"Your extra-time request for {venue} wasn't approved. "
                    "Wrap up at your scheduled end time."
                )
            _send_push_to_user_sync(
                ba_user.id,
                title=title,
                body=body,
                data={
                    "screen": "shifts",
                    "eventUuid": str(getattr(event, "uuid", "")),
                    "kind": "extension_decision",
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("extension decision push failed ext=%s: %s", ext.id, exc)

    return {
        "ok": True,
        "already": False,
        "status": ext.status,
        "approved_minutes": ext.approved_minutes,
        "message": (
            f"Approved +{ext.approved_minutes} min." if approve
            else "Extension declined."
        ),
        **base,
    }
