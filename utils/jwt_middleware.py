"""JWT middleware that preserves CONN_MAX_AGE pooling while surviving stale DB sockets.

gqlauth's ``django_jwt_middleware`` loads the user via ``sync_to_async`` on
asgiref's shared thread-sensitive executor. That thread keeps a Django DB
connection across requests, but ``request_started`` (which resets
``CONN_HEALTH_CHECKS`` / ``health_check_done`` and enforces ``CONN_MAX_AGE``)
only fires on the ASGI request thread — so a Cloud SQL / proxy idle drop used
to leave a dead connection on the sync thread.

Previously this middleware called ``close_thread_connections()`` unconditionally
on every request, which prevented 500s but completely defeated
``DATABASES['default']['CONN_MAX_AGE']`` by forcing a fresh TCP + TLS + auth
handshake on every GraphQL call.

Now that ``DATABASES['default']['CONN_HEALTH_CHECKS'] = True`` is configured
properly on the database dict, calling ``close_if_unusable_or_obsolete()`` on
initialized connections for the current thread mirrors Django's request-boundary
maintenance:
  - resets ``health_check_done = False`` so Django runs its lightweight health
    check before the first query on the request (and skips it for the rest of
    the request);
  - closes connections that exceeded ``CONN_MAX_AGE``, drifted out of
    autocommit, or recorded unrecoverable errors;
  - keeps healthy, non-expired connections open for reuse across requests;
  - still retries once after force-closing on ``OperationalError`` /
    ``InterfaceError`` in case the socket drops mid-query.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from asgiref.sync import sync_to_async
from django.db import connection, connections
from django.db.utils import InterfaceError, OperationalError
from django.http import HttpRequest
from django.utils.decorators import sync_and_async_middleware
from gqlauth.core.middlewares import USER_OR_ERROR_KEY, UserOrError, get_user_or_error


def _ensure_usable_connection() -> None:
    """Run Django's request-boundary connection maintenance on this thread.

    Reuses healthy pooled connections within ``CONN_MAX_AGE`` while ensuring
    ``CONN_HEALTH_CHECKS`` runs once per request on the asgiref sync thread.
    """
    for conn in connections.all(initialized_only=True):
        try:
            conn.close_if_unusable_or_obsolete()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def _get_user_or_error_resilient(request: HttpRequest) -> UserOrError:
    _ensure_usable_connection()
    try:
        return get_user_or_error(request)
    except (OperationalError, InterfaceError):
        # Connection died between the health check and the query (race with proxy).
        # Close and retry once.
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
