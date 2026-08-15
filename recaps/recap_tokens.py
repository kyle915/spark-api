"""Signed share token for a single recap (legacy or custom).

A recap can be shared via a link that needs no Spark login — the signed
token IS the authorization. Mirrors ``recaps.report_tokens`` (campaign
report) but with its own salt so a campaign-report token can't be
replayed here, and vice versa.

* Signer: :class:`django.core.signing.TimestampSigner`
* Salt: ``recaps.share.v1``
* Payload: ``legacy:<id>`` or ``custom:<id>`` (the integer PK)
* Expiry: 365 days — recap links get pasted into brand decks / emails.
"""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

_RECAP_TOKEN_SALT = "recaps.share.v1"

RECAP_TOKEN_MAX_AGE_SECONDS = 365 * 24 * 60 * 60

RecapKind = str  # "legacy" | "custom"


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_RECAP_TOKEN_SALT)


def make_recap_token(kind: RecapKind, recap_id: int) -> str:
    """Issue a signed share token for one recap."""
    if kind not in ("legacy", "custom"):
        raise ValueError(f"unknown recap kind: {kind}")
    return _signer().sign(f"{kind}:{int(recap_id)}")


def verify_recap_token(
    token: str, *, max_age: int | None = RECAP_TOKEN_MAX_AGE_SECONDS
) -> tuple[RecapKind, int]:
    """Verify + parse a recap share token; return ``(kind, recap_id)``.

    Raises :class:`SignatureExpired` / :class:`BadSignature` the same way
    the campaign-report token does. Pass ``max_age=None`` to skip expiry.
    """
    payload = _signer().unsign(token, max_age=max_age)
    kind, _, raw_id = payload.partition(":")
    if kind not in ("legacy", "custom") or not raw_id:
        raise BadSignature("malformed recap share payload")
    return kind, int(raw_id)


__all__ = [
    "make_recap_token",
    "verify_recap_token",
    "RECAP_TOKEN_MAX_AGE_SECONDS",
    "BadSignature",
    "SignatureExpired",
]
