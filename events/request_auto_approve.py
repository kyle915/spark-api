"""Per-tenant request auto-approve (``Tenant.auto_approve_requests``).

A brand with the flag on lands every brand-submitted request approved — the
public spark-form and the client portal login — and its approval email goes
to the requestor with the brand's ``request_approved_cc_emails`` on CC instead
of the whole-Ignite roll-up. Torch keeps its own public-form gate
(``events.torch_portal``). Recaps are not affected.
"""

from __future__ import annotations

from events.event_confirmations import public_page_base
from events.routing import merge_always_include_internal, suppress_cc
from tenants.models import parse_recipient_emails


def tenant_auto_approves_requests(tenant) -> bool:
    return bool(getattr(tenant, "auto_approve_requests", False))


def tenant_request_cc_emails(tenant) -> list[str]:
    """The brand's auto-approval CC list; empty when auto-approve is off.

    Standing internal addresses are appended whenever the flag is on, including
    when the brand column is blank, so a new client cannot omit them.
    """
    if not tenant_auto_approves_requests(tenant):
        return []
    return merge_always_include_internal(
        suppress_cc(
            parse_recipient_emails(getattr(tenant, "request_approved_cc_emails", ""))
        )
    )


def auto_approved_request_lists(
    tenant, requestor_email: str | None
) -> tuple[list[str], list[str]]:
    """To: requestor (or the first CC when there is none). CC: the brand list."""
    cc = tenant_request_cc_emails(tenant)
    requestor = (requestor_email or "").strip()
    if requestor:
        key = requestor.lower()
        return [requestor], [e for e in cc if e.lower() != key]
    if not cc:
        return [], []
    return [cc[0]], cc[1:]


def request_form_url(tenant) -> str | None:
    """The brand's public request form on client.igniteproductions.co."""
    slug = (getattr(tenant, "request_url_name", None) or "").strip()
    if not slug:
        return None
    return f"{public_page_base()}/spark-form/{slug}"
