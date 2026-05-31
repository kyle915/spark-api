"""Signed share token for the Client Campaign Report.

A campaign report can be shared via a link that needs no Spark login —
the signed token IS the authorization. Mirrors the events approval-flow
token (``events.views._signer`` / ``make_approval_token``) but with its
own salt so a token minted here can't be replayed against the approval
endpoint, and vice versa.

* Signer: :class:`django.core.signing.TimestampSigner` (HMAC over the
  payload + a timestamp, keyed by ``SECRET_KEY``).
* Salt: ``reports.campaign.v1`` — unique to this flow.
* Payload: the request id (as a string).
* Expiry: generous (~365 days) — these links get pasted into brand
  decks / emails and are expected to keep resolving for the life of the
  campaign. :func:`verify_report_token` also accepts ``max_age=None`` so a
  caller can opt out of expiry entirely.
"""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

# Salt scopes the signer to the campaign-report flow ONLY. Never reuse
# this string elsewhere — that's how Django keeps a stolen token from
# another feature (approval, magic-link, password reset) from being
# replayed here.
_REPORT_TOKEN_SALT = "reports.campaign.v1"

# 365 days. Report links live in brand decks and recap emails for the
# length of an activation program; a year is comfortable headroom without
# leaving a leaked link valid forever.
REPORT_TOKEN_MAX_AGE_SECONDS = 365 * 24 * 60 * 60


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_REPORT_TOKEN_SALT)


def make_report_token(request_id: int) -> str:
    """Issue a signed share token for a request's campaign report."""
    return _signer().sign(str(int(request_id)))


def verify_report_token(
    token: str, *, max_age: int | None = REPORT_TOKEN_MAX_AGE_SECONDS
) -> int:
    """Verify + parse a share token; return the request id.

    Raises :class:`django.core.signing.SignatureExpired` when the token is
    older than ``max_age`` and :class:`django.core.signing.BadSignature`
    when it's malformed / tampered. Pass ``max_age=None`` to skip the
    expiry check entirely (still verifies the signature).
    """
    payload = _signer().unsign(token, max_age=max_age)
    return int(payload)


__all__ = [
    "make_report_token",
    "verify_report_token",
    "REPORT_TOKEN_MAX_AGE_SECONDS",
    "BadSignature",
    "SignatureExpired",
]
