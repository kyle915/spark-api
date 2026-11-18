"""Anti-scraping / anti-bulk-abuse guardrails (Django app label ``abuse``).

Brand users (role=client, tenant-scoped) must not be able to point an AI
agent or bot at Spark and bulk-export platform data. This package holds the
shared, reusable pieces; the GraphQL wiring lives in ``graphql_extension``.

Public function API (stable — other apps, e.g. the read-only MCP server,
should call these rather than touching the models directly):

Rate limiting (Postgres-backed, shared by every Cloud Run instance)::

    from utils.abuse.limiter import hit, ahit, RateLimitExceeded

    result = hit("mcp:user:42", limit=60, window_seconds=60)
    if not result.allowed:
        ...  # result.retry_after seconds until the window resets

    # or raise a GraphQL-friendly error in one call:
    await ahit("mcp:user:42", limit=60, window_seconds=60, raise_on_limit=True)

Audit log (who / what / when / ip / ua for exports, bulk actions, signed
download URLs, ambassador reads, login throttles)::

    from utils.abuse.audit import record_audit, arecord_audit

    record_audit(kind="export", operation="mcp.listRecaps", user=user,
                 tenant_id=tenant.id, params={"first": 50}, row_count=50,
                 request=request)

Usage rollups (per user per hour; feeds the daily detector)::

    from utils.abuse.usage import record_usage  # see docstring

Request identity helpers::

    from utils.abuse.identity import client_ip, user_agent, resolve_identity

Everything fails OPEN on infrastructure errors (DB down, table missing):
a broken limiter must never take the product down. Each such failure is
logged at WARNING on the ``spark.abuse`` logger.
"""
