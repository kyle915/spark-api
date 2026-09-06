"""JWT middleware that survives stale DB connections on the ASGI sync thread.

gqlauth's ``django_jwt_middleware`` loads the user via ``sync_to_async`` on
asgiref's shared thread-sensitive executor. That thread keeps a Django DB
connection across requests, but ``request_started`` (which resets
``CONN_HEALTH_CHECKS`` / ``health_check_done``) only fires on the ASGI request
thread — so a Cloud SQL / proxy idle drop leaves a dead connection on the sync
thread.

Symptom in prod: ``OperationalError:django.request:log_response`` on
``/api/.../graphql/clients`` with the traceback ending in
``gqlauth`` → ``token.get_user_instance()`` → ``asgiref`` sync thread handler
(x236 as of 2026-09). Same root class as the off-loop ``fresh_db_connection``
helper in :mod:`utils.db` and ``ambassadors.push._db_sync``.

This middleware mirrors gqlauth's API but closes the thread's DB connection
before the JWT user ORM load (and retries once on OperationalError) so a
dead handle never 500s the GraphQL request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from asgiref.sync import sync_to_async
from django.db import connection
from django.db.utils import InterfaceError, OperationalError
from django.http import HttpRequest
from django.utils.decorators import sync_and_async_middleware
from gqlauth.core.middlewares import USER_OR_ERROR_KEY, UserOrError, get_user_or_error

from utils.db import close_thread_connections


def _ensure_usable_connection() -> None:
    """Drop any connection this asgiref executor thread may be holding.

    Force-close (not just ``health_check_done = False``): until
    ``DATABASES['default']['CONN_HEALTH_CHECKS']`` is True, Django skips
    ``close_if_health_check_failed`` entirely, and even with it enabled the
    request_started reset never reaches this thread. Closing is cheap —
    Django reopens lazily on the next query — and matches the proven push
    / ``fresh_db_connection`` pattern.
    """
    close_thread_connections()


def _get_user_or_error_resilient(request: HttpRequest) -> UserOrError:
    _ensure_usable_connection()
    try:
        return get_user_or_error(request)
    except (OperationalError, InterfaceError):
        # Connection died between close and the query (race with proxy).
        # Close again and retry once.
        connection.close()
        return get_user_or_error(request)


@sync_and_async_middleware
def django_jwt_middleware(get_response: Callable):
    """Drop-in replacement for ``gqlauth.core.middlewares.django_jwt_middleware``."""

    def logic(request: HttpRequest) -> None:
        if not hasattr(request, USER_OR_ERROR_KEY):
            user_or_error = _get_user_or_error_resilient(request)
            setattr(request, USER_OR_ERROR_KEY, user_or_error)

    if asyncio.iscoroutinefunction(get_response):
        async_logic = sync_to_async(logic)

        async def middleware(request: HttpRequest):
            await async_logic(request)
            return await get_response(request)

    else:

        def middleware(request: HttpRequest):  # type: ignore[misc]
            logic(request)
            return get_response(request)

    return middleware
