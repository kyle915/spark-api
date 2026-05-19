"""ID-token verifiers for Sign in with Apple and Google ID token sign-in.

Used by the mobile ``appleSignIn`` / ``googleSignIn`` mutations. Each
verifier accepts a raw ID token string and returns a small dataclass
of trusted claims (email, name) — or raises ``OAuthVerificationError``.

Both verifiers do real cryptographic verification of the JWT signature
against the provider's published public keys. Do not weaken this — an
unverified id_token lets anyone log in as any email.

Apple keys are JWKs at ``settings.APPLE_OAUTH_KEYS_URL``; we cache them
for 1 hour to avoid hammering Apple on every sign-in. Google is handled
by ``google.oauth2.id_token`` which manages its own key cache.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

import httpx
import jwt
from django.conf import settings
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


class OAuthVerificationError(Exception):
    """Raised when an OAuth id_token fails verification."""


@dataclass
class OAuthIdentity:
    """Trusted claims pulled from a verified id_token."""

    provider: str  # "apple" | "google"
    subject: str  # the provider's stable user id (``sub`` claim)
    email: str
    email_verified: bool
    first_name: str | None = None
    last_name: str | None = None


# ---------------------------------------------------------------------------
# Apple
# ---------------------------------------------------------------------------


# Module-level cache so we don't refetch Apple's JWKs every request. Apple
# rotates keys infrequently but with no advance notice, so we keep the
# cache short.
_APPLE_JWK_CACHE: dict[str, object] = {"client": None, "fetched_at": 0.0}
_APPLE_JWK_TTL_SECONDS = 60 * 60


def _apple_jwk_client() -> PyJWKClient:
    now = time.time()
    client = _APPLE_JWK_CACHE.get("client")
    fetched_at = _APPLE_JWK_CACHE.get("fetched_at") or 0.0
    if client and (now - float(fetched_at)) < _APPLE_JWK_TTL_SECONDS:
        return client  # type: ignore[return-value]
    url = settings.APPLE_OAUTH_KEYS_URL
    new_client = PyJWKClient(url, cache_keys=True)
    _APPLE_JWK_CACHE["client"] = new_client
    _APPLE_JWK_CACHE["fetched_at"] = now
    return new_client


def verify_apple_id_token(
    identity_token: str,
    *,
    audiences: Iterable[str] | None = None,
    name_hint: dict[str, str] | None = None,
) -> OAuthIdentity:
    """Verify an Apple identity_token and return trusted claims.

    Apple only includes ``email``/``name`` on the *first* sign-in. On
    subsequent sign-ins those claims are absent, and the mobile client
    must pass the name fields it cached locally via ``name_hint``.
    """
    if not identity_token:
        raise OAuthVerificationError("Missing Apple identity token.")

    accepted = list(audiences) if audiences else list(settings.APPLE_OAUTH_AUDIENCES)
    if not accepted:
        raise OAuthVerificationError("APPLE_OAUTH_AUDIENCES is not configured.")

    try:
        client = _apple_jwk_client()
        signing_key = client.get_signing_key_from_jwt(identity_token)
        claims = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=accepted,
            issuer=settings.APPLE_OAUTH_ISSUER,
            options={"require": ["sub", "aud", "iss", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise OAuthVerificationError(f"Invalid Apple identity token: {exc}") from exc
    except Exception as exc:
        # Network failures fetching JWKs land here.
        raise OAuthVerificationError(
            f"Could not verify Apple identity token: {exc}"
        ) from exc

    subject = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    email_verified = bool(claims.get("email_verified")) or claims.get(
        "email_verified"
    ) == "true"
    if not subject:
        raise OAuthVerificationError("Apple identity token missing 'sub' claim.")
    if not email:
        # Apple only includes email on first auth; the mobile client should
        # have cached it locally and resent it, but if not we still need
        # *something* to find/create the user.
        raise OAuthVerificationError(
            "Apple identity token did not include an email. "
            "Have the user sign out of the app and try again."
        )

    first = last = None
    if name_hint:
        first = (name_hint.get("first_name") or name_hint.get("firstName") or None)
        last = (name_hint.get("last_name") or name_hint.get("lastName") or None)

    return OAuthIdentity(
        provider="apple",
        subject=str(subject),
        email=email,
        email_verified=email_verified,
        first_name=first,
        last_name=last,
    )


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


def verify_google_id_token(
    id_token_str: str,
    *,
    audiences: Iterable[str] | None = None,
) -> OAuthIdentity:
    """Verify a Google id_token and return trusted claims."""
    if not id_token_str:
        raise OAuthVerificationError("Missing Google id_token.")

    # Imported lazily — google-auth is a multi-module package and we don't
    # want to pay its import cost on every Django boot.
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token as g_id_token

    accepted = list(audiences) if audiences else list(settings.GOOGLE_OAUTH_AUDIENCES)
    if not accepted:
        raise OAuthVerificationError("GOOGLE_OAUTH_AUDIENCES is not configured.")

    # ``verify_oauth2_token`` accepts a single audience, but the Google
    # client allows the audience claim to be either a string or any of
    # several values. We loop so we can accept iOS + web client ids.
    last_error: Exception | None = None
    claims: dict | None = None
    for aud in accepted:
        try:
            claims = g_id_token.verify_oauth2_token(
                id_token_str, g_requests.Request(), audience=aud
            )
            break
        except ValueError as exc:
            last_error = exc
            continue
    if claims is None:
        raise OAuthVerificationError(
            f"Invalid Google id_token: {last_error}"
        )

    issuer = claims.get("iss")
    if issuer not in ("https://accounts.google.com", "accounts.google.com"):
        raise OAuthVerificationError(f"Unexpected Google issuer: {issuer!r}")

    subject = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    if not subject or not email:
        raise OAuthVerificationError(
            "Google id_token missing required claims (sub/email)."
        )

    return OAuthIdentity(
        provider="google",
        subject=str(subject),
        email=email,
        email_verified=bool(claims.get("email_verified")),
        first_name=claims.get("given_name") or None,
        last_name=claims.get("family_name") or None,
    )


# Re-exported for callers that just want a quick health probe.
def fetch_apple_keys() -> dict:
    """Refresh and return Apple's JWK set. Useful from a shell to debug."""
    try:
        resp = httpx.get(settings.APPLE_OAUTH_KEYS_URL, timeout=10.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise OAuthVerificationError(f"Could not fetch Apple JWKs: {exc}") from exc
