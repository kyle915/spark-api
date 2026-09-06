"""Stale-connection resilience for JWT user load on the ASGI sync thread."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.db.utils import OperationalError
from django.test import RequestFactory, SimpleTestCase

from gqlauth.core.middlewares import USER_OR_ERROR_KEY, UserOrError
from utils.jwt_middleware import _get_user_or_error_resilient, django_jwt_middleware


class JwtMiddlewareStaleConnectionTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_closes_thread_connection_before_user_load(self):
        request = self.factory.get("/api/v615204/graphql/clients")
        ok = UserOrError()
        with (
            patch("utils.jwt_middleware.close_thread_connections") as close_all,
            patch(
                "utils.jwt_middleware.get_user_or_error",
                return_value=ok,
            ) as get_user,
        ):
            result = _get_user_or_error_resilient(request)

        close_all.assert_called_once()
        get_user.assert_called_once_with(request)
        self.assertIs(result, ok)

    def test_retries_once_after_operational_error(self):
        request = self.factory.get("/api/v615204/graphql/clients")
        ok = UserOrError()
        with (
            patch("utils.jwt_middleware.close_thread_connections"),
            patch(
                "utils.jwt_middleware.get_user_or_error",
                side_effect=[OperationalError("the connection is closed"), ok],
            ) as get_user,
            patch("utils.jwt_middleware.connection") as conn,
        ):
            result = _get_user_or_error_resilient(request)

        self.assertEqual(get_user.call_count, 2)
        conn.close.assert_called_once()
        self.assertIs(result, ok)

    def test_middleware_attaches_user_or_error(self):
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


class ConnHealthChecksSettingTests(SimpleTestCase):
    def test_conn_health_checks_lives_on_databases_dict(self):
        # Top-level CONN_HEALTH_CHECKS is ignored by Django; only the
        # DATABASES entry enables connection.health_check_enabled.
        from django.conf import settings

        self.assertIs(
            settings.DATABASES["default"].get("CONN_HEALTH_CHECKS"),
            True,
        )
