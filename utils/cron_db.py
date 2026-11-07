"""Shared DB hygiene for management commands / cron HTTP handlers.

Cloud SQL idle drops on long-lived Cloud Run instances leave Django holding
a closed psycopg handle on threads that never see ``request_started``. Cron
commands and ThreadPool workers must close connections before and after ORM
work — same contract as :func:`utils.db.fresh_db_connection`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

from utils.db import close_thread_connections, fresh_db_connection

_T = TypeVar("_T")


@contextmanager
def cron_db_session() -> Iterator[None]:
    """Close thread connections around a cron / command body."""
    close_thread_connections()
    try:
        yield
    finally:
        close_thread_connections()


def run_cron_db(fn: Callable[[], _T]) -> _T:
    """Run ``fn`` on a fresh DB connection (close before + after)."""
    return fresh_db_connection(fn)()
