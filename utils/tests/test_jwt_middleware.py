"""Stale-connection resilience for JWT user load on the ASGI sync thread."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.db.utils import OperationalError
from django.test import SimpleTestCase, RequestFactory

from gqlauth.core.middlewares import USER_OR_ERROR_KEY, UserOrError
from utils.jwt_middleware import _get_user_or_error_resilient, django_jwt_middleware


class JwtMiddlewareStaleConnectionTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_retries_once_after_operational_error(self):
        request = self.factory.get("/api/v1/graphql/clients")
        ok = UserOrError()
        with (
            patch("utils.jwt_middleware._ensure_usable_connection") as ensure,
            patch(
                "utils.jwt_middleware.get_user_or_error",
                side_effect=[OperationalError("the connection is closed"), ok],
            ) as get_user,
            patch("utils.jwt_middleware.connection") as conn,
        ):
            result = _get_user_or_error_resilient(request)

        ensure.assert_called_once()
        self.assertEqual(get_user.call_count, 2)
        conn.close.assert_called_once()
        self.assertIs(result, ok)

    def test_middleware_attaches_user_or_error(self):
        request = self.factory.get("/api/v1/graphql/clients")
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
