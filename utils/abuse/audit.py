"""Audit log writer. Never raises: a failed audit write is logged, not fatal.

    record_audit(kind, operation, *, user=None, identity=None, tenant_id=None,
                 params=None, row_count=None, success=True, detail="",
                 request=None, ip=None, ua=None) -> AuditLogEntry | None
    await arecord_audit(...)  # same signature

``params`` should be a SUMMARY (argument names, list lengths, filters) — never
raw tokens, passwords or full id lists. Use :func:`summarize_args` to build one.
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import sync_to_async
from django.db import DatabaseError

from .identity import Identity, client_ip, resolve_identity, user_agent

logger = logging.getLogger("spark.abuse")

_SECRET_KEYS = {"password", "password1", "password2", "newpassword", "oldpassword",
                "token", "refreshtoken", "code", "secret", "otp"}


def summarize_args(value: Any, *, _depth: int = 0) -> Any:
    """Shrink argument values for the audit log: lists become their length
    (plus the first 3 scalar items), secrets are redacted, strings truncated."""
    if _depth > 4:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:40]:
            norm = str(k).replace("_", "").lower()
            out[str(k)] = "[redacted]" if norm in _SECRET_KEYS else summarize_args(v, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        head = [summarize_args(v, _depth=_depth + 1) for v in list(value)[:3]
                if not isinstance(v, (dict, list, tuple))]
        return {"len": len(value), "head": head}
    if isinstance(value, str):
        return value[:120]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:120]


def record_audit(kind: str, operation: str, *, user=None, identity: Identity | None = None,
                 tenant_id: int | None = None, params: dict | None = None,
                 row_count: int | None = None, success: bool = True, detail: str = "",
                 request=None, ip: str | None = None, ua: str | None = None):
    from .models import AuditLogEntry

    try:
        if identity is None and user is not None:
            identity = resolve_identity(user)
        user_id = identity.user_id if identity else getattr(user, "pk", None)
        entry = AuditLogEntry.objects.create(
            user_id=user_id,
            user_email=(identity.email if identity else getattr(user, "email", "") or "")[:254],
            is_admin=bool(identity.is_admin) if identity else False,
            tenant_id=tenant_id if tenant_id is not None else (identity.tenant_id if identity else None),
            kind=kind,
            operation=operation[:120],
            params=params or {},
            row_count=row_count,
            success=success,
            detail=(detail or "")[:500],
            ip=(ip if ip is not None else (client_ip(request) if request is not None else ""))[:64],
            user_agent=(ua if ua is not None else (user_agent(request) if request is not None else ""))[:300],
        )
        return entry
    except (DatabaseError, ValueError, TypeError):
        logger.warning("audit write failed for %s/%s", kind, operation, exc_info=True)
        return None


arecord_audit = sync_to_async(record_audit)
