"""Signed share token for the Client Live campaign page.

Same cookie-free, signed-token pattern as the campaign report
(``recaps/report_tokens.py``) — the token IS the authorization — but with its
own salt so a client-live token can't be replayed against the report or
approval endpoints. Payload = the tenant id; a generous expiry so a link
pasted into a client email keeps resolving for the life of the program.
"""
from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

# Unique to the client-live flow — never reuse elsewhere.
_CLIENT_LIVE_SALT = "client.live.v1"

# 365 days — client links live in emails for the length of a program.
CLIENT_LIVE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_CLIENT_LIVE_SALT)


def make_client_live_token(tenant_id: int) -> str:
    """Issue a signed share token for a tenant's live client page."""
    return _signer().sign(str(int(tenant_id)))


def verify_client_live_token(
    token: str, *, max_age: int | None = CLIENT_LIVE_MAX_AGE_SECONDS
) -> int:
    """Verify + parse a client-live token; return the tenant id. Raises
    SignatureExpired / BadSignature (like the report token)."""
    return int(_signer().unsign(token, max_age=max_age))


__all__ = [
    "make_client_live_token",
    "verify_client_live_token",
    "CLIENT_LIVE_MAX_AGE_SECONDS",
    "BadSignature",
    "SignatureExpired",
]
