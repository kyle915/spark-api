"""offboard_client_users: Liquid Death layoffs (Oct 2026)."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from events import models as event_models
from events.tests.base import EventsGraphQLTestCase
from tenants.models import TenantedUser


@pytest.mark.django_db
class TestOffboardClientUsers(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        self.ld = self.create_tenant(name="Liquid Death", request_url_name="ighn-liquid-death")
        self.request_type = self.create_request_type(name="Retail Sampling", tenant=self.ld)
        self.lauren = self.create_user(
            username="l.giaccio@liquiddeath.com", email="l.giaccio@liquiddeath.com", role=self.roles["client"]
        )
        self.kristyn = self.create_user(
            username="k.williams@liquiddeath.com", email="K.Williams@liquiddeath.com", role=self.roles["client"]
        )
        TenantedUser.objects.create(user=self.kristyn, tenant=self.ld, created_by=self.system_user)
        self.ld.recap_recipient_emails = "k.williams@liquiddeath.com, ross@liquiddeath.com"
        self.ld.default_external_rmm = self.kristyn
        self.ld.save()
        now = timezone.now()
        self.upcoming = self._request(now + timedelta(days=3))
        self.past = self._request(now - timedelta(days=30))

    def _request(self, when):
        return event_models.Request.objects.create(
            name="Kroger",
            address="1 Main St, Fresno, CA 93701",
            request_type=self.request_type,
            tenant=self.ld,
            created_by=self.system_user,
            rmm_asigned=self.kristyn,
            date=when,
        )

    def _run(self, **opts):
        out = StringIO()
        call_command(
            "offboard_client_users",
            tenant_slug="ighn-liquid-death",
            emails="k.williams@liquiddeath.com, t.reed@liquiddeath.com",
            reassign_to="l.giaccio@liquiddeath.com",
            stdout=out,
            **opts,
        )
        return out.getvalue()

    def test_dry_run_reports_and_writes_nothing(self):
        out = self._run()
        assert "upcoming_requests=1" in out
        assert "t.reed@liquiddeath.com: no Spark user" in out
        self.kristyn.refresh_from_db()
        assert self.kristyn.is_active

    def test_apply_deactivates_and_hands_upcoming_work_to_lauren(self):
        self._run(apply=True)
        self.kristyn.refresh_from_db()
        self.ld.refresh_from_db()
        self.upcoming.refresh_from_db()
        self.past.refresh_from_db()
        assert not self.kristyn.is_active
        assert not TenantedUser.objects.filter(user=self.kristyn, is_active=True).exists()
        assert self.upcoming.rmm_asigned_id == self.lauren.id
        assert self.past.rmm_asigned_id == self.kristyn.id
        assert self.ld.recap_recipient_emails == "ross@liquiddeath.com"
        assert self.ld.default_external_rmm_id is None

    def test_cannot_hand_work_to_someone_being_offboarded(self):
        with pytest.raises(CommandError):
            call_command(
                "offboard_client_users",
                tenant_slug="ighn-liquid-death",
                emails="k.williams@liquiddeath.com",
                reassign_to="k.williams@liquiddeath.com",
                stdout=StringIO(),
            )

    def test_unknown_tenant_errors(self):
        with pytest.raises(CommandError, match="tenant-not-found"):
            call_command(
                "offboard_client_users",
                tenant_slug="no-such-tenant",
                emails="k.williams@liquiddeath.com",
                stdout=StringIO(),
            )

    def test_dry_run_flags_heir_without_membership(self):
        out = self._run()
        assert "heir l.giaccio@liquiddeath.com: active=True membership=False" in out
