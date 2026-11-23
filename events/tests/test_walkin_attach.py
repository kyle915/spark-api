"""Safe orphan walk-up → Tracker request attach helpers + GraphQL."""

from __future__ import annotations

import datetime as _dt
import uuid

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone as dj_tz

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event, Request
from events.walkin_link import (
    WalkinAttachError,
    attach_all_safe_orphans,
    attach_orphan_to_request,
    safe_attach_suggestions_for_tenant,
)
from recaps.models import Recap


ATTACH_ONE = """
mutation AttachOne($input: AttachWalkupOrphanInput!) {
  attachWalkupOrphan(input: $input) {
    success
    message
    linkedCount
    orphanEventUuid
    requestUuid
  }
}
"""

ATTACH_ALL = """
mutation AttachAll($input: AttachSafeWalkupOrphansInput!) {
  attachSafeWalkupOrphans(input: $input) {
    success
    message
    linkedCount
  }
}
"""

MISSING_WITH_SUGGEST = """
query Missing($tenantId: ID, $lookbackDays: Int!) {
  missingRecapEvents(tenantId: $tenantId, lookbackDays: $lookbackDays) {
    eventUuid
    requestUuid
    suggestedWalkupAttach {
      orphanEventUuid
      orphanEventName
      requestUuid
      confidence
    }
  }
}
"""

ADDR = "12731 South Saginaw Street, Grand Blanc, MI 48439"


@pytest.mark.django_db(transaction=True)
class TestWalkinAttachHelpers(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Walkin Attach {uid}")
        self.req_type = self.create_request_type("Retail", self.tenant)
        self.req_status = self.create_request_status("Approved", self.tenant)
        self.day = dj_tz.now().replace(
            hour=12, minute=0, second=0, microsecond=0
        ) - _dt.timedelta(days=2)

    def _due_request_with_shell(self, *, address: str, name: str):
        req = Request.objects.create(
            name=name,
            address=address,
            date=self.day,
            start_time=self.day.replace(hour=14),
            end_time=self.day.replace(hour=18),
            tenant=self.tenant,
            status=self.req_status,
            request_type=self.req_type,
            created_by=self.system_user,
        )
        shell = Event.objects.create(
            tenant=self.tenant,
            name=name,
            address=address,
            date=self.day,
            start_time=self.day.replace(hour=14),
            end_time=self.day.replace(hour=18),
            request=req,
            created_by=self.system_user,
        )
        return req, shell

    def _filed_orphan(self, *, address: str, name: str):
        orphan = Event.objects.create(
            tenant=self.tenant,
            name=name,
            address=address,
            date=self.day,
            created_by=self.system_user,
        )
        Recap.objects.create(
            name=name,
            event=orphan,
            submited_at=dj_tz.now(),
            total_engagements=12,
            created_by=self.system_user,
        )
        return orphan

    def test_safe_suggestion_and_one_click_attach(self):
        req, shell = self._due_request_with_shell(address=ADDR, name="Kroger")
        orphan = self._filed_orphan(address=ADDR, name="Kroger walk-up")

        suggestions = safe_attach_suggestions_for_tenant(self.tenant, days_back=30)
        assert len(suggestions) == 1
        assert suggestions[0].orphan_event_id == orphan.id
        assert suggestions[0].request_id == req.id

        attached = attach_orphan_to_request(orphan.id, req.id, days_back=30)
        orphan.refresh_from_db()
        assert orphan.request_id == req.id
        assert attached.request_uuid == str(req.uuid)

        from recaps.filed import events_missing_filed_recap

        still_due = events_missing_filed_recap(
            Event.objects.filter(pk=shell.id)
        ).exists()
        assert still_due is False

    def test_ambiguous_pair_is_refused(self):
        req, _shell = self._due_request_with_shell(address=ADDR, name="Kroger")
        orphan_a = self._filed_orphan(address=ADDR, name="Kroger A")
        orphan_b = self._filed_orphan(address=ADDR, name="Kroger B")

        suggestions = safe_attach_suggestions_for_tenant(self.tenant, days_back=30)
        assert suggestions == []

        with pytest.raises(WalkinAttachError, match="unique"):
            attach_orphan_to_request(orphan_a.id, req.id, days_back=30)
        orphan_a.refresh_from_db()
        orphan_b.refresh_from_db()
        assert orphan_a.request_id is None
        assert orphan_b.request_id is None

    def test_attach_all_safe_orphans(self):
        req, _shell = self._due_request_with_shell(address=ADDR, name="Kroger")
        orphan = self._filed_orphan(address=ADDR, name="Kroger walk-up")

        result = attach_all_safe_orphans(self.tenant, days_back=30)
        assert result.linked == 1
        orphan.refresh_from_db()
        assert orphan.request_id == req.id

    def test_bulk_attach_skips_parked_orphans(self, monkeypatch):
        req, _shell = self._due_request_with_shell(address=ADDR, name="Kroger")
        orphan = self._filed_orphan(address=ADDR, name="Kroger walk-up")
        monkeypatch.setattr(
            "events.walkin_link._parked_orphan_ids",
            lambda: frozenset({orphan.id}),
        )

        result = attach_all_safe_orphans(self.tenant, days_back=30)
        assert result.linked == 0
        orphan.refresh_from_db()
        assert orphan.request_id is None
        assert safe_attach_suggestions_for_tenant(self.tenant, days_back=30) == []


@pytest.mark.django_db(transaction=True)
class TestWalkinAttachGraphQL(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.system_user = self.get_system_user()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Walkin GQL {uid}")
        self.admin = self.create_user(
            username=f"admin-walkin-{uid}",
            email=f"admin-walkin-{uid}@igniteproductions.co",
            role=self.roles["spark_admin"],
        )
        self.req_type = self.create_request_type("Retail", self.tenant)
        self.req_status = self.create_request_status("Approved", self.tenant)
        self.day = dj_tz.now().replace(
            hour=12, minute=0, second=0, microsecond=0
        ) - _dt.timedelta(days=2)
        self.req = Request.objects.create(
            name="Kroger",
            address=ADDR,
            date=self.day,
            start_time=self.day.replace(hour=14),
            end_time=self.day.replace(hour=18),
            tenant=self.tenant,
            status=self.req_status,
            request_type=self.req_type,
            created_by=self.system_user,
        )
        self.shell = Event.objects.create(
            tenant=self.tenant,
            name="Kroger",
            address=ADDR,
            date=self.day,
            start_time=self.day.replace(hour=14),
            end_time=self.day.replace(hour=18),
            request=self.req,
            created_by=self.system_user,
        )
        self.orphan = Event.objects.create(
            tenant=self.tenant,
            name="Kroger walk-up",
            address=ADDR,
            date=self.day,
            created_by=self.system_user,
        )
        Recap.objects.create(
            name="Kroger walk-up",
            event=self.orphan,
            submited_at=dj_tz.now(),
            total_engagements=9,
            created_by=self.system_user,
        )

    @pytest.mark.asyncio
    async def test_missing_query_surfaces_suggestion(self):
        result = await self._execute_mutation(
            MISSING_WITH_SUGGEST,
            {
                "tenantId": str(self.tenant.id),
                "lookbackDays": 30,
            },
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None, result.errors
        rows = result.data["missingRecapEvents"]
        shell_row = next(
            (r for r in rows if r["eventUuid"] == str(self.shell.uuid)), None
        )
        assert shell_row is not None
        sug = shell_row["suggestedWalkupAttach"]
        assert sug is not None
        assert sug["orphanEventUuid"] == str(self.orphan.uuid)
        assert sug["requestUuid"] == str(self.req.uuid)
        assert sug["confidence"] == "safe"

    @pytest.mark.asyncio
    async def test_attach_one_mutation(self):
        result = await self._execute_mutation(
            ATTACH_ONE,
            {
                "input": {
                    "orphanEventUuid": str(self.orphan.uuid),
                    "requestUuid": str(self.req.uuid),
                    "lookbackDays": 30,
                }
            },
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None, result.errors
        out = result.data["attachWalkupOrphan"]
        assert out["success"] is True
        assert out["linkedCount"] == 1
        await sync_to_async(self.orphan.refresh_from_db)()
        assert self.orphan.request_id == self.req.id

    @pytest.mark.asyncio
    async def test_attach_all_mutation(self):
        result = await self._execute_mutation(
            ATTACH_ALL,
            {
                "input": {
                    "tenantId": str(self.tenant.id),
                    "lookbackDays": 30,
                }
            },
            self.endpoint_path,
            user=self.admin,
        )
        assert result.errors is None, result.errors
        out = result.data["attachSafeWalkupOrphans"]
        assert out["success"] is True
        assert out["linkedCount"] == 1
        await sync_to_async(self.orphan.refresh_from_db)()
        assert self.orphan.request_id == self.req.id
