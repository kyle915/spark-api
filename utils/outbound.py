"""Context-local kill switch for every outbound side effect.

Inside ``suppress_outbound()`` no email, push, Cloud Task, calendar sync,
Sheets mirror, recap export or job-board auto-post leaves this thread of
work. Built for data seeding (``seed_brand_group_demo``) that must run the
real write paths without telling anyone.

A ``ContextVar`` rather than a module-level flag or ``mock.patch`` because
Cloud Run serves concurrent requests in one process: a global switch would
also mute a real BA filing a recap at the same moment.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

logger = logging.getLogger(__name__)

_BLOCKED: ContextVar[list[str] | None] = ContextVar("outbound_blocked", default=None)


@contextmanager
def suppress_outbound() -> Iterator[list[str]]:
    """Block outbound side effects; yields the list of blocked attempts."""
    blocked: list[str] = []
    token = _BLOCKED.set(blocked)
    try:
        yield blocked
    finally:
        _BLOCKED.reset(token)


def outbound_suppressed() -> bool:
    return _BLOCKED.get() is not None


def block_if_suppressed(kind: str, detail: object = "") -> bool:
    """True (and records the attempt) when outbound is suppressed."""
    blocked = _BLOCKED.get()
    if blocked is None:
        return False
    entry = f"{kind}: {detail}" if detail != "" else kind
    blocked.append(entry)
    logger.info("outbound suppressed — %s", entry)
    return True
