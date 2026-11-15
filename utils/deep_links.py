"""Tenant-aware deep links into the Spark web app.

The web app keeps one *selected* client (tenant) per browser, and a list URL
such as ``/walkups`` or ``/recaps/list`` says nothing about which client it
is for. An emailed "Open Walk-ups" button therefore opened whatever client
the staff member last had selected — a Breakaway alert showing Liquid
Death's queue.

``with_tenant`` stamps ``?tenant=<tenant uuid>`` onto such a link. The front
end (TenantGuard) resolves it before the page renders and only ever against
the tenants the signed-in user can already switch to, so the parameter is a
hint, not an access grant: every query stays server-scoped as before.

Record permalinks (``/recap/view-custom/:uuid``, ``/request/view/:uuid``,
``/event/view/:uuid``) do not need it — those pages switch to the record's
own tenant when they load.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TENANT_LINK_PARAM = "tenant"


def _tenant_uuid(tenant=None, tenant_id=None) -> str:
    uuid = getattr(tenant, "uuid", None) if tenant is not None else None
    if not uuid and tenant_id:
        try:
            from tenants.models import Tenant

            uuid = (
                Tenant.objects.filter(id=tenant_id)
                .values_list("uuid", flat=True)
                .first()
            )
        except Exception:  # noqa: BLE001 — a link must never fail a mailer
            uuid = None
    return str(uuid) if uuid else ""


def with_tenant(url: str | None, tenant=None, *, tenant_id=None) -> str:
    """``url`` with ``tenant=<uuid>`` added to its query string.

    Returns ``url`` unchanged when it is empty, when no tenant can be
    resolved, or when it already carries a ``tenant`` parameter. Existing
    query parameters and the fragment are preserved.
    """
    url = url or ""
    if not url:
        return url
    uuid = _tenant_uuid(tenant, tenant_id)
    if not uuid:
        return url
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == TENANT_LINK_PARAM for key, _ in query):
        return url
    query.append((TENANT_LINK_PARAM, uuid))
    return urlunsplit(parts._replace(query=urlencode(query)))
