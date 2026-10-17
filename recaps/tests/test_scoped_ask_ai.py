"""Ask AI scoped to one recap or one event."""

from unittest.mock import patch

import pytest
from django.utils import timezone

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models
from recaps import models as recap_models
from recaps.scoped_ai import build_event_scope, build_recap_scope


QUERY = """
query Ask($tenantId: ID!, $question: String!, $recapId: ID, $eventId: ID) {
  tenantAiAnswer(
    tenantId: $tenantId
    question: $question
    recapId: $recapId
    eventId: $eventId
  ) {
    ok
    answer
    reason
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestScopedAskAi(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(name="Scoped Brand")
        self.other = self.create_tenant(name="Other Store Co")
        self.admin = self.create_user(
            username="scoped-admin",
            email="scoped-admin@test.com",
            role=self.roles["spark_admin"],
        )

    def _recap(self, tenant, quote: str, *, name: str):
        event = self.create_event(name=name, tenant=tenant, address="1 Main")
        event_models.Event.objects.filter(id=event.id).update(
            date=timezone.now(), notes=f"notes for {name}"
        )
        event.refresh_from_db()
        recap = recap_models.Recap.objects.create(
            name=name,
            event=event,
            total_engagements=12,
            approved=True,
            submited_at=timezone.now(),
            created_by=self.system_user,
        )
        recap_models.ConsumerFeedback.objects.create(
            recap=recap,
            quotes=quote,
            feedback=quote,
        )
        return recap, event

    def test_recap_block_excludes_the_other_store(self):
        mine, _event = self._recap(self.tenant, "ALPHA SHOPPER QUOTE", name="Mine")
        self._recap(self.other, "BETA SHOPPER QUOTE", name="Theirs")
        block = build_recap_scope(self.tenant.id, str(mine.id))
        assert block is not None
        assert "ALPHA SHOPPER QUOTE" in block.text
        assert "BETA SHOPPER QUOTE" not in block.text
        assert block.kind == "recap"

    def test_event_block_stays_on_that_event(self):
        _mine, event = self._recap(self.tenant, "ON THIS EVENT", name="Here")
        other_event = self.create_event(name="Elsewhere", tenant=self.tenant)
        recap_models.Recap.objects.create(
            name="Other stop",
            event=other_event,
            total_engagements=3,
            approved=True,
            created_by=self.system_user,
        )
        recap_models.ConsumerFeedback.objects.create(
            recap=recap_models.Recap.objects.get(name="Other stop"),
            quotes="OTHER EVENT QUOTE",
        )
        block = build_event_scope(self.tenant.id, str(event.id))
        assert block is not None
        assert "ON THIS EVENT" in block.text
        assert "OTHER EVENT QUOTE" not in block.text

    def test_other_tenant_recap_is_hidden(self):
        theirs, _event = self._recap(self.other, "SECRET", name="Hidden")
        assert build_recap_scope(self.tenant.id, str(theirs.id)) is None

    def test_photo_metadata_is_included_without_a_url(self):
        recap, _event = self._recap(self.tenant, "photo stop", name="Photos")
        file_type = FileType.objects.create(name="image", created_by=self.system_user)
        category = recap_models.FileRecapCategory.objects.create(
            name="Sampling photos",
            tenant=self.tenant,
            created_by=self.system_user,
        )
        recap_models.RecapFile.objects.create(
            name="shelf-left.jpg",
            file="recap_files/shelf-left.jpg",
            recap=recap,
            file_type=file_type,
            file_recap_category=category,
            created_by=self.system_user,
        )
        block = build_recap_scope(self.tenant.id, str(recap.uuid))
        assert block is not None
        assert "Sampling photos" in block.text
        assert "shelf-left.jpg" in block.text
        assert "http" not in block.text

    @pytest.mark.asyncio
    async def test_graphql_sends_only_the_recap_and_calls_the_model_on_ask(self):
        mine = await self._async_recap()
        with patch(
            "recaps.report_types.generate_structured_answer",
            return_value=("Shoppers mentioned the display.", None),
        ) as generate:
            result = await self._execute_query_authenticated(
                QUERY,
                {
                    "tenantId": str(self.tenant.id),
                    "question": "What did shoppers say?",
                    "recapId": str(mine.id),
                },
                self.admin,
                self.endpoint_path,
            )
        assert result.errors is None, result.errors
        assert result.data["tenantAiAnswer"]["ok"] is True
        prompt = generate.call_args[0][1]
        system = generate.call_args[0][0]
        assert "ALPHA ONLY" in prompt
        assert "BETA ONLY" not in prompt
        assert "ONE filed recap" in system
        assert generate.call_count == 1

    async def _async_recap(self):
        from asgiref.sync import sync_to_async

        def _build():
            mine, _event = self._recap(self.tenant, "ALPHA ONLY", name="Ask mine")
            self._recap(self.other, "BETA ONLY", name="Ask theirs")
            return mine

        return await sync_to_async(_build)()
