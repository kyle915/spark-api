"""The ONE place that says which legal documents admin and brand users must
accept, and at which version.

Bumping a version here re-prompts every admin/client user on their next
request: the GraphQL gate (``legal.graphql_extension``) blocks data calls
until they accept the new version in the web clickwrap.

The text itself lives in the web app (spark-front-client ``src/legal/*.ts``,
served at the paths below). When the text changes there, bump the matching
version here in the same release and write a one-line ``CHANGE_SUMMARY`` so
the re-prompt can say what changed.

Tests (and an emergency hotfix) can override the table with the
``LEGAL_DOCUMENT_VERSIONS`` / ``LEGAL_CHANGE_SUMMARY`` settings.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

TERMS = "terms"
PRIVACY = "privacy"
AUP = "aup"


@dataclass(frozen=True)
class LegalDocument:
    key: str
    title: str
    path: str  # path on the web app (spark-front-client public route)
    version: str  # ISO date of the effective version, e.g. "2026-11-17"


# Effective date November 17, 2026 (spark-front-client #944).
_DEFAULT_VERSIONS: dict[str, str] = {
    TERMS: "2026-11-17",
    AUP: "2026-11-17",
    PRIVACY: "2026-11-17",
}

_DOCS: tuple[tuple[str, str, str], ...] = (
    (TERMS, "Terms of Service", "/terms-of-service"),
    (AUP, "Acceptable Use Policy", "/acceptable-use-policy"),
    (PRIVACY, "Privacy Policy", "/privacy-policy"),
)

# "What changed" line shown on a RE-prompt (someone who accepted an older
# version). Empty for the first version: nobody has accepted anything yet.
_DEFAULT_CHANGE_SUMMARY = ""

DOCUMENT_KEYS: tuple[str, ...] = tuple(k for k, _t, _p in _DOCS)


def _versions() -> dict[str, str]:
    override = getattr(settings, "LEGAL_DOCUMENT_VERSIONS", None) or {}
    return {**_DEFAULT_VERSIONS, **override}


def current_documents() -> list[LegalDocument]:
    versions = _versions()
    return [LegalDocument(k, t, p, versions[k]) for k, t, p in _DOCS]


def current_versions() -> dict[str, str]:
    return {d.key: d.version for d in current_documents()}


def current_version() -> str:
    """One string for the whole bundle: the newest document version. The web
    client echoes it back to ``acceptLegalTerms`` so a stale tab can't accept
    text it never showed."""
    return max(current_versions().values())


def change_summary() -> str:
    value = getattr(settings, "LEGAL_CHANGE_SUMMARY", None)
    return _DEFAULT_CHANGE_SUMMARY if value is None else str(value)


def title_for(key: str) -> str:
    for k, t, _p in _DOCS:
        if k == key:
            return t
    return key
