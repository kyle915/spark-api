"""System Health query — Ignite-admin observability snapshot (backend
errors + server info). Admin-gated; everyone else gets null."""

from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync

from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestSystemHealth(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        from django.contrib.auth import get_user_model
        from tenants.models import Role

        self.system_user = self.get_system_user()
        User = get_user_model()
        admin_role, _ = Role.objects.get_or_create(
            slug=Role.SPARK_ADMIN_SLUG,
            defaults={"name": "Spark Admin", "created_by": self.system_user},
        )
        client_role, _ = Role.objects.get_or_create(
            slug=Role.CLIENT_SLUG,
            defaults={"name": "Client", "created_by": self.system_user},
        )
        self.admin = User.objects.create_user(
            username="sh-admin", email="admin@igniteproductions.co",
            role=admin_role, is_active=True,
        )
        self.client_user = User.objects.create_user(
            username="sh-client", email="client@brand.com",
            role=client_role, is_active=True,
        )
        # Seed one recorded error so the snapshot has content.
        from digest.models import BackendErrorEvent

        BackendErrorEvent.objects.create(
            signature="ValueError:recaps.mutations:create",
            message="boom", location="recaps/mutations.py:12", count=3,
        )

    def _health(self, as_user):
        from tenants.schema import QuerySpark

        info = SimpleNamespace(
            context=SimpleNamespace(request=SimpleNamespace(user=as_user))
        )
        return async_to_sync(QuerySpark.system_health)(QuerySpark(), info)

    def test_admin_sees_health_snapshot(self):
        health = self._health(self.admin)
        assert health is not None
        assert health.distinct_signatures == 1
        assert health.recent_errors[0].count == 3
        assert health.git_sha  # server info folded in

    def test_client_gets_null(self):
        assert self._health(self.client_user) is None
