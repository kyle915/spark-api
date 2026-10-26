"""Torch public spark-form auto-approve + recipient lists.

ONLY the public form at /spark-form/keee-torch-thc (tenant keee-torch-thc /
torch-thc) auto-approves. Liquid Death, Feel Free, Girl Beer, KKC, and every
other public spark-form stay pending until a human approves.
"""

from __future__ import annotations

from events.routing import merge_always_include_internal

TORCH_PUBLIC_FORM_SLUGS = frozenset({"keee-torch-thc"})
TORCH_TENANT_SLUGS = frozenset({"torch", "torch-thc", "keee-torch-thc"})
TORCH_TENANT_NAMES = frozenset({"torch", "torch thc", "torch-thc", "torch thc"})

# Request approved: requestor + Liberty + Ignite ops list. Not Girl Beer.
TORCH_REQUEST_APPROVED_CC: tuple[str, ...] = (
    "liberty@torchdrinks.com",
    "kyle@igniteproductions.co",
    "harris@igniteproductions.co",
    "events@igniteproductions.co",
    "myriant@igniteproductions.co",
    "keis@igniteproductions.co",
    "nevena@igniteproductions.co",
)

# Recap submitted: requestor + Liberty + events + Nevena only.
# Do not blast Kyle / Harris / myriant / keis unless they are the requestor.
TORCH_RECAP_SUBMIT_CC: tuple[str, ...] = (
    "liberty@torchdrinks.com",
    "events@igniteproductions.co",
    "nevena@igniteproductions.co",
)

# Ignite addresses excluded from Torch-related Spark emails.
TORCH_SUPPRESS_EMAILS: frozenset[str] = frozenset({
    "baesha@igniteproductions.co",
})

# Off Torch recap mail only (submitted, approved, review, sign-off,
# comments, suspect-numbers). Shared lists keep this address for every
# other brand, and Torch mail that is not a recap (request approved,
# field-marketing plan submit, clock-in) keeps it too.
TORCH_RECAP_SKIP_EMAILS: frozenset[str] = frozenset({
    "junior@igniteproductions.co",
})


def suppress_torch_emails(
    emails: list[str] | tuple[str, ...],
    tenant=None,
) -> list[str]:
    """Strip ``TORCH_SUPPRESS_EMAILS`` when ``tenant`` is Torch (or omitted)."""
    if tenant is not None and not is_torch_tenant(tenant):
        return list(emails)
    return [
        e for e in emails
        if (e or "").strip().lower() not in TORCH_SUPPRESS_EMAILS
    ]


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def is_torch_public_form_slug(request_url_name: str | None) -> bool:
    return _norm(request_url_name) in TORCH_PUBLIC_FORM_SLUGS


def is_torch_tenant(tenant) -> bool:
    """True only for the Torch THC brand (id 17 in prod)."""
    if tenant is None:
        return False
    slug = _norm(getattr(tenant, "slug", None))
    url_name = _norm(getattr(tenant, "request_url_name", None))
    name = _norm(getattr(tenant, "name", None))
    if slug in TORCH_TENANT_SLUGS:
        return True
    if url_name in TORCH_PUBLIC_FORM_SLUGS or url_name in TORCH_TENANT_SLUGS:
        return True
    if name in TORCH_TENANT_NAMES:
        return True
    return False


def _drop_torch_recap_skips(
    emails: list[str] | tuple[str, ...],
) -> list[str]:
    return [
        email
        for email in emails
        if (email or "").strip().lower() not in TORCH_RECAP_SKIP_EMAILS
    ]


def omit_torch_recap_skips(
    emails: list[str] | tuple[str, ...],
    tenant,
) -> list[str]:
    """Drop Torch recap skips when ``tenant`` is Torch.

    A shared recipient list is left intact for every other brand. Passing
    a non-Torch tenant (or none) returns the list unchanged.
    """
    if not is_torch_tenant(tenant):
        return list(emails)
    return _drop_torch_recap_skips(emails)


def should_auto_approve_public_request(
    request_url_name: str | None, tenant
) -> bool:
    """Public createRequestByUrl auto-approves Torch form + Torch tenant only.

    Both sides must match so a tampered tenant_id on another brand's form
    (or keee-torch-thc posted against Liquid Death) cannot flip status.
    """
    return is_torch_public_form_slug(request_url_name) and is_torch_tenant(tenant)


def _dedupe_emails(*groups: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for email in group:
            normalized = (email or "").strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(normalized)
    return out


def torch_request_approved_lists(
    requestor_email: str | None,
) -> tuple[list[str], list[str]]:
    """To: requestor (or first CC). CC: Liberty + Ignite Torch list."""
    requestor = (requestor_email or "").strip()
    cc = merge_always_include_internal(
        suppress_torch_emails(_dedupe_emails(TORCH_REQUEST_APPROVED_CC))
    )
    if requestor:
        requestor_key = requestor.lower()
        cc = [e for e in cc if e.lower() != requestor_key]
        return [requestor], cc
    if not cc:
        return [], []
    return [cc[0]], cc[1:]


def torch_recap_submit_lists(
    requestor_emails: list[str] | tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """To: requestor(s). CC: Liberty + events + Nevena. No Kyle/Harris blast."""
    requestors = _drop_torch_recap_skips(
        suppress_torch_emails(_dedupe_emails(requestor_emails))
    )
    seen = {e.lower() for e in requestors}
    cc = [
        e
        for e in _drop_torch_recap_skips(
            suppress_torch_emails(_dedupe_emails(TORCH_RECAP_SUBMIT_CC))
        )
        if e.lower() not in seen
    ]
    if requestors:
        return requestors, cc
    if not cc:
        return [], []
    return [cc[0]], cc[1:]
