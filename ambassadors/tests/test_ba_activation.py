"""BA activation dashboard backend: admin-gated per-tenant activation rows +
the resend-welcome service the mutation wraps."""
import io

import pytest
from asgiref.sync import async_to_sync
from django.core import mail

from ambassadors.models import Ambassador, AmbassadorEvent
from ambassadors.services import reset_ba_welcome_and_email
from events.models import Event
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestBaActivation(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        from tenants.models import Role
        from tenants.tests.base import ensure_role
        from utils.utils import ROLE_ID
        self.role = ensure_role(
            "Ambassador", slug=Role.AMBASSADOR_SLUG,
            pk=ROLE_ID.Ambassadors, created_by=self.system_user)
        self.tenant = self.create_tenant(name="Feel Free")
        etype = self.create_event_type(name="Field Sampling", tenant=self.tenant)
        status = self.create_event_status(name="Approved", tenant=self.tenant)
        self.event = Event.objects.create(
            name="Miami — Wynwood · 7/2", tenant=self.tenant,
            event_type=etype, status=status, created_by=self.system_user,
        )
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.ba_user = User.objects.create_user(
            username="maria", email="gabyiglesiaspr@gmail.com",
            first_name="Maria", last_name="Iglesias", role=self.role,
            is_active=True,
        )
        self.amb = Ambassador.objects.create(
            user=self.ba_user, is_active=True,
            created_by=self.ba_user, updated_by=self.ba_user,
        )
        AmbassadorEvent.objects.create(
            ambassador=self.amb, event=self.event, tenant=self.tenant,
            is_approved=True, created_by=self.ba_user, updated_by=self.ba_user,
        )

    def _rows(self, as_user):
        from types import SimpleNamespace

        from ambassadors.queries import AmbassadorEventQueries

        info = SimpleNamespace(
            context=SimpleNamespace(request=SimpleNamespace(user=as_user))
        )
        return async_to_sync(AmbassadorEventQueries.tenant_ba_activation)(
            AmbassadorEventQueries(), info, tenant_id=str(self.tenant.id)
        )

    def test_admin_sees_booked_bas_with_signin_state(self):
        rows = self._rows(self.system_user)  # superuser fixture
        assert len(rows) == 1
        row = rows[0]
        assert row.email == "gabyiglesiaspr@gmail.com"
        assert row.signed_in is False
        assert row.bookings == 1

    def test_non_admin_gets_nothing(self):
        rows = self._rows(self.ba_user)
        assert rows == []

    def test_reset_service_sends_welcome_and_forces_change(self):
        msg = reset_ba_welcome_and_email("gabyiglesiaspr@gmail.com")
        assert "Welcome email sent" in msg
        self.ba_user.refresh_from_db()
        assert self.ba_user.requires_password_change is True
        assert len(mail.outbox) == 1
        assert "Welcome to Spark" in mail.outbox[0].subject

    def test_reset_service_unknown_email_raises(self):
        with pytest.raises(ValueError, match="User not found"):
            reset_ba_welcome_and_email("nobody@nowhere.io")
