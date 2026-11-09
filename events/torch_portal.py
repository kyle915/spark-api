"""Torch public spark-form auto-approve + recipient lists.

ONLY the public form at /spark-form/keee-torch-thc (tenant keee-torch-thc /
torch-thc) auto-approves. Liquid Death, Feel Free, Girl Beer, KKC, and every
other public spark-form stay pending until a human approves.
"""

from __future__ import annotations


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
    from events.outbound_policy import apply_outbound_policy

    requestor = (requestor_email or "").strip()
    cc = apply_outbound_policy(
        _dedupe_emails(TORCH_REQUEST_APPROVED_CC),
        tenant=None,
        kind="generic",
        merge_internal=True,
        exclude_email=requestor or None,
    )
    if requestor:
        return [requestor], cc
    if not cc:
        return [], []
    return [cc[0]], cc[1:]

