"""Signed share token for a brand's public program page.

Same shape as ``recaps.report_tokens`` (one campaign) but scoped to a whole
brand, with its own salt so a campaign or recap token can never be replayed
here. The token alone is not enough: the page only resolves while the brand
has ``program_share_enabled`` switched on, which is how a link is revoked.
"""

from __future__ import annotations

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

_PROGRAM_TOKEN_SALT = "reports.program.v1"
PROGRAM_TOKEN_MAX_AGE_SECONDS = 365 * 24 * 60 * 60


def _signer() -> TimestampSigner:
    return TimestampSigner(salt=_PROGRAM_TOKEN_SALT)


def make_program_token(tenant_id: int) -> str:
    return _signer().sign(str(int(tenant_id)))


def verify_program_token(
    token: str, *, max_age: int | None = PROGRAM_TOKEN_MAX_AGE_SECONDS
) -> int:
    return int(_signer().unsign(token, max_age=max_age))


__all__ = [
    "BadSignature",
    "SignatureExpired",
    "make_program_token",
    "verify_program_token",
]
