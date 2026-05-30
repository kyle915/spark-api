"""Per-event receipt-upload tokenization.

The consumer receipt-upload link is `/<token>`-addressable: an admin shows
the link (and a QR of it) for an event, a shopper opens it, and the public
endpoints resolve the token back to the event + tenant.

We mint the token with `django.core.signing.TimestampSigner` — the exact
same primitive the public *approval* flow uses (`events/views.py`), just
with its own pinned salt so a token from one flow can't be replayed against
the other. The signed payload is the event id, so the token is:

  * stable for a given event (no DB column to migrate / backfill), and
  * tamper-proof + scoped (HMAC over the payload with the project
    SECRET_KEY, namespaced by salt).

Unlike the approval link (14-day expiry — it's an actionable admin email),
a receipt-upload link is a long-lived QR an event team may print and reuse
across a multi-week activation, so the default lifetime is generous and the
resolver also accepts `max_age=None` (no expiry) verification.
"""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

# Pin the salt to THIS flow. Do not reuse elsewhere — that's what stops a
# stolen approval / magic-link / password-reset token from being replayed
# against the receipt endpoints (same posture as events.views).
_RECEIPT_TOKEN_SALT = "receipts.event_upload.v1"

# Default lifetime for a minted upload link. Generous on purpose: a printed
# QR for a sampling activation may live for weeks. ~180 days. The verify
# helper can be called with a different max_age (incl. None = never expire)
# if a caller wants stricter / looser behavior.
DEFAULT_RECEIPT_TOKEN_MAX_AGE_SECONDS = 180 * 24 * 60 * 60


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_RECEIPT_TOKEN_SALT)


def make_event_receipt_token(event_id: int) -> str:
    """Issue a stable signed token for an event's public upload link."""
    return _signer().sign(str(int(event_id)))


def verify_event_receipt_token(
    token: str,
    *,
    max_age: int | None = DEFAULT_RECEIPT_TOKEN_MAX_AGE_SECONDS,
) -> int:
    """Verify a token and return the event id.

    Raises ``django.core.signing.BadSignature`` (or its subclass
    ``SignatureExpired``) if the token is invalid, tampered, or expired —
    callers map those to a 4xx response.
    """
    payload = _signer().unsign(token, max_age=max_age)
    return int(payload)


__all__ = [
    "BadSignature",
    "SignatureExpired",
    "DEFAULT_RECEIPT_TOKEN_MAX_AGE_SECONDS",
    "make_event_receipt_token",
    "verify_event_receipt_token",
]
