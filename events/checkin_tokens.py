"""Signed session token for the public web check-in flow.

The shareable check-in LINK carries the event's ``walkup_code`` (a short,
human-typeable code an admin generates for the event — see
``ambassadors/walkup.py``). Possession of that code is the authorization to
*start* a check-in, exactly like the mobile walk-up flow.

Once a BA identifies themselves on the public page, the identify endpoint mints
one of these signed **session** tokens binding that browser to a single
(event, ambassador) pair. Every subsequent public call (clock in/out, request a
photo upload URL, submit the recap) carries the session token, so those actions
can't be performed for an arbitrary event/BA — the token IS the per-session
authorization. Same cookie-free, signed-token pattern as the client-live page
(``events/client_live_tokens.py``) and campaign report, with its own salt so a
session token can't be replayed against the other public endpoints.

Payload = ``"<event_id>:<ambassador_id>"``. Short-lived by design — a check-in
session is a single shift, not a program.
"""
from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

# Unique to the web check-in flow — never reuse elsewhere.
_CHECKIN_SESSION_SALT = "checkin.session.v1"

# 2 days — comfortably covers a shift plus a late/next-morning recap, matching
# the walk-up code's own expiry grace (ambassadors/walkup.py).
CHECKIN_SESSION_MAX_AGE_SECONDS = 2 * 24 * 60 * 60


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_CHECKIN_SESSION_SALT)


def make_checkin_session_token(event_id: int, ambassador_id: int) -> str:
    """Issue a signed session token binding a browser to one (event, BA)."""
    return _signer().sign(f"{int(event_id)}:{int(ambassador_id)}")


def read_checkin_session_token(
    token: str, *, max_age: int | None = CHECKIN_SESSION_MAX_AGE_SECONDS
) -> tuple[int, int]:
    """Verify + parse a session token; return ``(event_id, ambassador_id)``.

    Raises ``SignatureExpired`` / ``BadSignature`` (like the report token); a
    malformed payload raises ``ValueError`` so callers can treat it as invalid.
    """
    raw = _signer().unsign(token, max_age=max_age)
    event_part, _, amb_part = raw.partition(":")
    if not event_part or not amb_part:
        raise ValueError("Malformed check-in session token.")
    return int(event_part), int(amb_part)


__all__ = [
    "make_checkin_session_token",
    "read_checkin_session_token",
    "CHECKIN_SESSION_MAX_AGE_SECONDS",
    "BadSignature",
    "SignatureExpired",
]
