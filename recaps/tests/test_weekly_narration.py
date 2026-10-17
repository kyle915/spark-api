"""Weekly narration stays on bucket numbers and does not email."""

from datetime import date
from unittest.mock import patch

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.weekly_narration import (
    compose_narration,
    fallback_narration,
    narration_facts,
    numbers_are_grounded,
    upsert_narration,
)
from tenants.models import TenantWeeklyNarration


QUERY = """
query Narration($tenantId: ID!) {
  tenantWeeklyNarration(tenantId: $tenantId) {
    weekStart
    weekEnd
    body
    filedRecaps
  }
}
"""

WEEK_START = date(2026, 10, 5)
WEEK_END = date(2026, 10, 11)


def _facts():
    return {
        "week_start": "2026-10-05",
        "week_end": "2026-10-11",
        "filed_recaps": 4,
        "buckets": [
            {
                "key": "reach",
                "title": "Consumers sampled",
                "metric": "40",
                "detail": "40 consumers sampled across 2 events.",
            }
        ],
        "labels": "filed_recaps is separate from consumers sampled.",
    }


def test_invented_number_is_rejected():
    facts = _facts()
    assert numbers_are_grounded("40 consumers sampled and 4 filed recaps.", facts)
    assert not numbers_are_grounded("We reached 999 consumers.", facts)
    assert not numbers_are_grounded("Conversion was 12%.", facts)


def test_fallback_uses_only_fact_sentences():
    text = fallback_narration(_facts())
    assert "4 filed recaps" in text
    assert "40 consumers sampled" in text
    assert numbers_are_grounded(text, _facts())


def test_compose_drops_an_invented_figure():
    body, source = compose_narration(
        _facts(),
        generate=lambda _system, _user: "The week produced 999 samples.",
    )
    assert source == "fallback"
    assert "999" not in body
    assert "4 filed recaps" in body


@pytest.mark.django_db(transaction=True)
class TestWeeklyNarrationJob(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Narration Brand")
        self.admin = self.create_user(
            username="narration-admin",
            email="narration-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="narration-client",
            email="narration-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        when = timezone.now().replace(
            year=2026, month=10, day=7, hour=15, minute=0, second=0, microsecond=0
        )
        event = self.create_event(name="Week stop", tenant=self.tenant)
        event_models.Event.objects.filter(id=event.id).update(date=when)
        recap_models.Recap.objects.create(
            name="Filed",
            event=event,
            total_engagements=8,
            approved=True,
            submited_at=when,
            created_by=self.system_user,
        )

    def test_facts_include_filed_count_and_do_not_email(self):
        facts = narration_facts(self.tenant.id, WEEK_START, WEEK_END)
        assert facts["filed_recaps"] == 1
        assert "emails" not in facts or facts.get("emails_sent", 0) == 0

    def test_dry_run_does_not_call_the_model_or_store_a_row(self):
        with patch("recaps.weekly_narration.generate_summary") as generate:
            summary = upsert_narration(
                self.tenant.id, WEEK_START, WEEK_END, apply=False
            )
        generate.assert_not_called()
        assert summary["written"] is False
        assert summary["emails_sent"] == 0
        assert TenantWeeklyNarration.objects.count() == 0

    def test_apply_stores_grounded_text(self):
        with patch(
            "recaps.weekly_narration.generate_summary",
            return_value="Quiet note with no digits.",
        ):
            summary = upsert_narration(
                self.tenant.id, WEEK_START, WEEK_END, apply=True
            )
        assert summary["written"] is True
        assert summary["emails_sent"] == 0
        row = TenantWeeklyNarration.objects.get(tenant=self.tenant)
        assert row.week_start == WEEK_START
        assert "1 filed recaps" in row.body or row.source == "model"
        assert numbers_are_grounded(row.body, row.facts)

    @pytest.mark.asyncio
    async def test_graphql_reads_the_stored_row_without_a_model_call(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(TenantWeeklyNarration.objects.create)(
            tenant=self.tenant,
            week_start=WEEK_START,
            week_end=WEEK_END,
            body="Week of 2026-10-05 to 2026-10-11: 1 filed recaps.",
            facts={"filed_recaps": 1},
            filed_recaps=1,
            source="fallback",
        )
        with patch("recaps.weekly_narration.generate_summary") as generate:
            admin = await self._execute_query_authenticated(
                QUERY,
                {"tenantId": str(self.tenant.id)},
                self.admin,
                self.endpoint_path,
            )
            client = await self._execute_query_authenticated(
                QUERY,
                {"tenantId": str(self.tenant.id)},
                self.client_user,
                self.endpoint_path,
            )
        generate.assert_not_called()
        assert admin.errors is None, admin.errors
        payload = admin.data["tenantWeeklyNarration"]
        assert payload["filedRecaps"] == 1
        assert "1 filed recaps" in payload["body"]
        assert client.errors is None, client.errors
        assert client.data["tenantWeeklyNarration"] is None


@override_settings(INTERNAL_CRON_SECRET="test-cron-secret-value-only-for-tests")
@pytest.mark.django_db
def test_cron_dry_run_is_the_default_and_does_not_apply():
    client = Client()
    with patch("digest.cron_views.call_command") as command:
        response = client.post(
            "/internal/cron/generate-weekly-narration",
            HTTP_X_CRON_SECRET="test-cron-secret-value-only-for-tests",
        )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["emails_sent"] == 0
    args = command.call_args[0]
    assert args[0] == "generate_weekly_narration"
    assert "--apply" not in args
