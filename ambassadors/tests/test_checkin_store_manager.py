"""Store manager name/phone ride along on the public check-in payload."""

from __future__ import annotations

import pytest
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models


@pytest.mark.django_db(transaction=True)
class TestCheckinStoreManager(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Liquid Death")
        self.actor = self.create_user(
            username="actor-sm@test.com",
            email="actor-sm@test.com",
            role=self.roles["spark_admin"],
        )
        self.event = self.create_event(
            name="HEB Congress",
            tenant=self.tenant,
            address="123 Congress Ave",
            date=timezone.now(),
        )

    def test_context_omits_store_manager_when_no_request(self):
        ctx = checkin_web.build_public_context(self.event)
        assert ctx["event"]["storeManagerName"] is None
        assert ctx["event"]["storeManagerPhone"] is None

    def test_context_passes_store_manager_from_request(self):
        req_type = self.create_request_type("Demo", self.tenant)
        req = event_models.Request.objects.create(
            name="HEB Congress request",
            address="123 Congress Ave",
            date=timezone.now(),
            tenant=self.tenant,
            request_type=req_type,
            store_manager_name="Pat Manager",
            store_manager_phone="(512) 555-0100",
            created_by=self.actor,
        )
        self.event.request = req
        self.event.save(update_fields=["request"])
        self.event.refresh_from_db()

        ctx = checkin_web.build_public_context(self.event)
        assert ctx["event"]["storeManagerName"] == "Pat Manager"
        assert ctx["event"]["storeManagerPhone"] == "(512) 555-0100"
