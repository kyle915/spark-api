"""audit_routing_state: read-only old vs new routed state report."""

from io import StringIO

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from events import models as em
from events.management.commands.audit_routing_state import legacy_extract_state_code
from events.routing import extract_state_code
from events.tests.base import EventsGraphQLTestCase
from recaps import models as recap_models


@pytest.mark.parametrize(
    "address,old,new",
    [
        ("13657 Washington St", "WA", None),
        ("3954A Peachtree Rd NE", "NE", None),
        ("3954A PEACHTREE ROAD NE, , BROOKHAVEN, GA30319", None, "GA"),
        ("100 Georgia Ave", "GA", None),
        ("1 Easton Way, Columbus, OH 43219", "OH", "OH"),
    ],
)
def test_legacy_parser_is_the_buggy_one(address, old, new):
    assert legacy_extract_state_code(address) == old
    assert extract_state_code(address) == new


@pytest.mark.django_db(transaction=True)
class TestAuditRoutingState(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.torch = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.ld = self.create_tenant(
            name="Liquid Death", slug="liquid-death", request_url_name="ighn-liquid-death"
        )
        self.spark = self.create_user(
            username="audit@test.com", email="audit@test.com", role=self.roles["spark_admin"]
        )

    def _approved_recap(self, tenant, address, name):
        event = self.create_event(name=name, tenant=tenant, address=address)
        return recap_models.Recap.objects.create(
            name=name,
            approved=True,
            approved_at=timezone.now(),
            client_notified_at=timezone.now(),
            event=event,
            created_by=self.spark,
            updated_by=self.spark,
        )

    def test_reports_changed_routings_without_writing(self):
        self._approved_recap(
            self.torch, "3954A PEACHTREE ROAD NE, , BROOKHAVEN, GA30319", "Kroger Brookhaven"
        )
        self._approved_recap(self.torch, "1 Easton Way, Columbus, OH 43219", "Kroger Easton")
        req_type = self.create_request_type(name="Event Activation", tenant=self.ld)
        req = em.Request.objects.create(
            name="Washington St Fest",
            address="13657 Washington St",
            request_type=req_type,
            tenant=self.ld,
            created_by=self.sys,
        )
        before = em.Request.objects.filter(pk=req.pk).values().get()

        out = StringIO()
        call_command("audit_routing_state", stdout=out)
        log = out.getvalue()

        assert "TORCH approved recaps: 2" in log
        assert "recipient set changes: 1 (mail already sent on 1)" in log
        assert "Kroger Brookhaven" in log and "old=None new=GA" in log
        assert "cesar@torchdrinks.com" in log
        assert "Kroger Easton" not in log
        assert "territory RMM changes (current map): 1" in log
        assert "old=WA new=None" in log and "pat@liquiddeath.com" in log
        assert em.Request.objects.filter(pk=req.pk).values().get() == before
        assert mail.outbox == []
