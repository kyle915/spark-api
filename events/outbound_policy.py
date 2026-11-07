"""Central outbound email policy (suppress / skip / always-include).

Torch and routing historically kept parallel frozensets; every notify path
must import from here so people changes stay one place. Tenant-specific
suppress lists can later move to DB without rewriting call sites.
"""

from __future__ import annotations

from events.routing import (
    ALWAYS_INCLUDE_INTERNAL_EMAILS,
    CC_SUPPRESS_EMAILS,
    merge_always_include_internal,
)
from events.torch_portal import (
    TORCH_RECAP_SKIP_EMAILS,
    TORCH_SUPPRESS_EMAILS,
    is_torch_tenant,
    omit_torch_recap_skips,
    suppress_torch_emails,
)

__all__ = [
    "ALWAYS_INCLUDE_INTERNAL_EMAILS",
    "CC_SUPPRESS_EMAILS",
    "TORCH_RECAP_SKIP_EMAILS",
    "TORCH_SUPPRESS_EMAILS",
    "apply_outbound_policy",
    "is_torch_tenant",
    "merge_always_include_internal",
    "omit_torch_recap_skips",
    "suppress_torch_emails",
]


def apply_outbound_policy(
    emails: list[str] | tuple[str, ...],
    *,
    tenant=None,
    kind: str = "generic",
) -> list[str]:
    """Filter + merge Ignite always-include for an outbound blast.

    ``kind``:
      - ``recap`` — also omit ``TORCH_RECAP_SKIP_EMAILS`` on Torch tenants
      - ``generic`` — Torch suppress only (Baesha) when tenant is Torch
    """
    cleaned = [e for e in emails if (e or "").strip()]
    cleaned = suppress_torch_emails(cleaned, tenant)
    if kind == "recap":
        cleaned = omit_torch_recap_skips(cleaned, tenant)
    # Global CC suppress (non-Torch paths that still use CC_SUPPRESS).
    suppress = {e.lower() for e in CC_SUPPRESS_EMAILS}
    cleaned = [e for e in cleaned if e.strip().lower() not in suppress]
    return merge_always_include_internal(cleaned)
