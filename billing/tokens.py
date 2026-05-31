"""Signed share token for an invoice's public link + PDF.

An invoice can be shared with a client via a link that needs no Spark login —
the signed token IS the authorization. Mirrors the campaign-report token
(``recaps.report_tokens``) and the events approval-flow token
(``events.views._signer``) but with its own salt so a token minted here can't
be replayed against the report / approval / receipt endpoints, and vice versa.

* Signer: :class:`django.core.signing.TimestampSigner` (HMAC over the payload
  + a timestamp, keyed by ``SECRET_KEY``).
* Salt: ``billing.invoice.v1`` — unique to this flow.
* Payload: the invoice's numeric pk (as a string).
* Expiry: generous (~365 days) — an invoice link gets emailed to a client and
  is expected to keep resolving while the bill is outstanding.
  :func:`verify_invoice_token` also accepts ``max_age=None`` so a caller can
  opt out of expiry entirely.
"""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

# Salt scopes the signer to the invoice flow ONLY. Never reuse this string
# elsewhere — that's how Django keeps a stolen token from another feature
# (report, approval, magic-link, password reset) from being replayed here.
_INVOICE_TOKEN_SALT = "billing.invoice.v1"

# 365 days. An invoice link lives in a client's inbox for the length of the
# payment cycle; a year is comfortable headroom without leaving a leaked link
# valid forever.
INVOICE_TOKEN_MAX_AGE_SECONDS = 365 * 24 * 60 * 60


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_INVOICE_TOKEN_SALT)


def make_invoice_token(invoice_id: int) -> str:
    """Issue a signed share token for an invoice's public link."""
    return _signer().sign(str(int(invoice_id)))


def verify_invoice_token(
    token: str, *, max_age: int | None = INVOICE_TOKEN_MAX_AGE_SECONDS
) -> int:
    """Verify + parse a share token; return the invoice id.

    Raises :class:`django.core.signing.SignatureExpired` when the token is
    older than ``max_age`` and :class:`django.core.signing.BadSignature` when
    it's malformed / tampered. Pass ``max_age=None`` to skip the expiry check
    entirely (still verifies the signature).
    """
    payload = _signer().unsign(token, max_age=max_age)
    return int(payload)


__all__ = [
    "make_invoice_token",
    "verify_invoice_token",
    "INVOICE_TOKEN_MAX_AGE_SECONDS",
    "BadSignature",
    "SignatureExpired",
]
