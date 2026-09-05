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
helper in :mod:`utils.db`.

This middleware mirrors gqlauth's API but forces a usability check (and one
reconnect retry) before the JWT user ORM load.
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


def _ensure_usable_connection() -> None:
    """Drop a dead connection on the current thread before ORM work.

    With ``CONN_HEALTH_CHECKS``, Django only re-pings when ``health_check_done``
    is False. That flag is cleared by ``request_started`` on the request thread,
    not on asgiref's sync executor — so we clear it ourselves here.
    """
    if connection.connection is not None:
        connection.health_check_done = False
    connection.close_if_unusable_or_obsolete()


def _get_user_or_error_resilient(request: HttpRequest) -> UserOrError:
    _ensure_usable_connection()
    try:
        return get_user_or_error(request)
    except (OperationalError, InterfaceError):
        # Connection died between the health check and the query (or the
        # health check itself was skipped). Close and retry once.
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
