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

        from tenants.tests.base import ensure_role

        self.system_user = self.get_system_user()
        User = get_user_model()
        # ensure_role converges on the name-unique constraint that fixtures
        # keep colliding on (0001_initial seeds these with NULL slugs).
        admin_role = ensure_role("Spark Admin", created_by=self.system_user)
        client_role = ensure_role("Client", created_by=self.system_user)
        self.admin = User.objects.create_user(
            username="sh-admin", email="admin@igniteproductions.co",
            role=admin_role, is_active=True,
        )
        self.client_user = User.objects.create_user(
            username="sh-client", email="client@brand.com",
            role=client_role, is_active=True,
        )
        # Seed one recorded error so the snapshot has content.
        from django.utils import timezone

        from digest.models import BackendErrorEvent, CronRun

        BackendErrorEvent.objects.create(
            signature="ValueError:recaps.mutations:create",
            message="boom", location="recaps/mutations.py:12", count=3,
        )
        # Seed a cron heartbeat so the automations section has content.
        CronRun.objects.create(
            name="activation-autopilot",
            last_run_at=timezone.now(),
            last_status=200,
            last_ok=True,
            run_count=7,
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
        assert health.walkup_orphan_filed_count >= 0
        assert health.walkup_orphan_filed_famus_count >= 0
        assert health.sheet_sync_error_tenant_count >= 0
        assert isinstance(health.sheet_sync_errors, list)

    def test_client_gets_null(self):
        assert self._health(self.client_user) is None

    # The admin web app posts this exact query (SparkSystemHealth.tsx) to
    # /graphql/clients. The resolver used to exist only on the Spark schema,
    # so the page failed with "Cannot query field 'systemHealth'" every load.
    FE_QUERY = """query SystemHealth {
      systemHealth {
        serverTime gitSha revision databaseOk
        errors24h errors7d distinctSignatures
        recentErrors { signature message location count firstSeen lastSeen }
        cronRuns { name lastRunAt lastOk lastStatus runCount hoursSince }
      }
    }"""

    def _run_on_clients_schema(self, as_user):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        return async_to_sync(self._execute_mutation)(
            self.FE_QUERY, {}, endpoint_path="/api/v1/graphql/clients", user=as_user
        )

    def test_the_web_apps_query_resolves_on_the_clients_schema(self):
        result = self._run_on_clients_schema(self.admin)
        assert result.errors is None, result.errors
        health = result.data["systemHealth"]
        assert health["distinctSignatures"] == 1
        assert health["recentErrors"][0]["count"] == 3
        assert health["cronRuns"][0]["name"] == "activation-autopilot"

    def test_clients_schema_still_hides_it_from_non_admins(self):
        result = self._run_on_clients_schema(self.client_user)
        assert result.errors is None, result.errors
        assert result.data["systemHealth"] is None
