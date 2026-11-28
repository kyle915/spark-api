"""Connection reuse & stale-socket resilience for JWT user load on ASGI."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from django.db.utils import InterfaceError, OperationalError
from django.test import RequestFactory, SimpleTestCase

from gqlauth.core.middlewares import USER_OR_ERROR_KEY, UserOrError
from utils.jwt_middleware import (
    _ensure_usable_connection,
    _get_user_or_error_resilient,
    django_jwt_middleware,
)


class JwtMiddlewareStaleConnectionTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_reuses_healthy_connection_via_close_if_unusable_or_obsolete(self):
        request = self.factory.get("/api/v615204/graphql/clients")
        ok = UserOrError()
        fake_conn = MagicMock()
        with (
            patch("utils.jwt_middleware.connections") as mock_connections,
            patch(
                "utils.jwt_middleware.get_user_or_error",
                return_value=ok,
            ) as get_user,
        ):
            mock_connections.all.return_value = [fake_conn]
            result = _get_user_or_error_resilient(request)

        mock_connections.all.assert_called_once_with(initialized_only=True)
        fake_conn.close_if_unusable_or_obsolete.assert_called_once_with()
        fake_conn.close.assert_not_called()
        get_user.assert_called_once_with(request)
        self.assertIs(result, ok)

    def test_falls_back_to_close_if_maintenance_check_raises(self):
        fake_conn = MagicMock()
        fake_conn.close_if_unusable_or_obsolete.side_effect = OperationalError(
            "broken socket"
        )
        with patch("utils.jwt_middleware.connections") as mock_connections:
            mock_connections.all.return_value = [fake_conn]
            _ensure_usable_connection()

        fake_conn.close_if_unusable_or_obsolete.assert_called_once_with()
        fake_conn.close.assert_called_once_with()

    def test_retries_once_after_operational_error(self):
        request = self.factory.get("/api/v615204/graphql/clients")
        ok = UserOrError()
        with (
            patch("utils.jwt_middleware._ensure_usable_connection") as ensure_usable,
            patch(
                "utils.jwt_middleware.get_user_or_error",
                side_effect=[OperationalError("the connection is closed"), ok],
            ) as get_user,
            patch("utils.jwt_middleware.connection") as conn,
        ):
            result = _get_user_or_error_resilient(request)

        ensure_usable.assert_called_once_with()
        self.assertEqual(get_user.call_count, 2)
        conn.close.assert_called_once_with()
        self.assertIs(result, ok)

    def test_retries_once_after_interface_error(self):
        request = self.factory.get("/api/v615204/graphql/clients")
        ok = UserOrError()
        with (
            patch("utils.jwt_middleware._ensure_usable_connection"),
            patch(
                "utils.jwt_middleware.get_user_or_error",
                side_effect=[InterfaceError("connection already closed"), ok],
            ) as get_user,
            patch("utils.jwt_middleware.connection") as conn,
        ):
            result = _get_user_or_error_resilient(request)

        self.assertEqual(get_user.call_count, 2)
        conn.close.assert_called_once_with()
        self.assertIs(result, ok)

    def test_middleware_attaches_user_or_error_sync_and_async(self):
        request = self.factory.get("/api/v615204/graphql/clients")
        ok = UserOrError()

        def get_response(_request):
            return MagicMock(status_code=200)

        middleware = django_jwt_middleware(get_response)
        with patch(
            "utils.jwt_middleware._get_user_or_error_resilient", return_value=ok
        ) as resilient:
            response = middleware(request)

        resilient.assert_called_once_with(request)
        self.assertIs(getattr(request, USER_OR_ERROR_KEY), ok)
        self.assertEqual(response.status_code, 200)

        async_request = self.factory.get("/api/v615204/graphql/clients")

        async def async_get_response(_request):
            return MagicMock(status_code=200)

        async_middleware = django_jwt_middleware(async_get_response)
        with patch(
            "utils.jwt_middleware._get_user_or_error_resilient", return_value=ok
        ) as async_resilient:
            async_response = asyncio.run(async_middleware(async_request))

        async_resilient.assert_called_once_with(async_request)
        self.assertIs(getattr(async_request, USER_OR_ERROR_KEY), ok)
        self.assertEqual(async_response.status_code, 200)


class ConnHealthChecksSettingTests(SimpleTestCase):
    def test_conn_health_checks_and_max_age_live_on_databases_dict(self):
        # Top-level CONN_HEALTH_CHECKS is ignored by Django; only the
        # DATABASES entry enables connection.health_check_enabled.
        from django.conf import settings

        self.assertIs(
            settings.DATABASES["default"].get("CONN_HEALTH_CHECKS"),
            True,
        )
        self.assertGreater(
            settings.DATABASES["default"].get("CONN_MAX_AGE", 0),
            0,
        )
