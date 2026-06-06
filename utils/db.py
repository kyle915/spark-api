"""Helpers for running ORM / DB work off the async event loop safely.

``sync_to_async(fn, thread_sensitive=False)`` runs ``fn`` in a pooled worker
thread. Django database connections are thread-local, and that pool thread
holds its connection across many requests — but Django's per-request
connection cleanup (``close_old_connections`` on the ``request_started`` /
``request_finished`` signals) only fires on the main request thread and never
reaches the pool thread. So a connection the database later closes (a Cloud SQL
idle timeout, a proxy recycle) is reused on the next call and raises
``the connection is closed`` / ``InterfaceError: connection already closed``.

This is also why ``close_old_connections()`` alone isn't enough here: with
``CONN_HEALTH_CHECKS`` the per-connection ``health_check_done`` flag is reset by
the request-started signal, which never fires in the pool thread — so a stale
connection can skip its health check and be reused regardless.

:func:`fresh_db_connection` wraps such a callable to **close the thread's
connection before and after** it runs. Django reopens lazily on the next query,
so the wrapped function always gets a healthy connection and never leaks a
stale one back into the pool. Only the off-loop pool threads are affected; the
main request path keeps its ``CONN_MAX_AGE`` pooling untouched.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar

from django.db import connection

_T = TypeVar("_T")


def fresh_db_connection(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Wrap a sync DB callable so it always runs on a fresh connection.

    Intended for callables handed to ``sync_to_async(..., thread_sensitive=False)``.
    The wrapped callable must fully materialize its result before returning
    (don't return an unevaluated QuerySet — the connection is closed on the way
    out).
    """

    @wraps(fn)
    def _wrapped(*args, **kwargs) -> _T:
        # Drop any stale connection this pooled thread inherited from a prior
        # call; the next ORM access opens a fresh one.
        connection.close()
        try:
            return fn(*args, **kwargs)
        finally:
            # Don't leak a connection back into the pool thread.
            connection.close()

    return _wrapped
