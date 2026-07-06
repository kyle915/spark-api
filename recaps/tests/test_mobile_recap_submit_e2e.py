"""End-to-end repro of the mobile legacy recap submit for Feel Free:
RecapMutationService driven exactly as the createRecap wrapper drives it,
with the input shaped exactly as RecapSubmitScreen builds it — eventId is
the event's UUID string."""
import io

import pytest
from asgiref.sync import async_to_sync

from ambassadors.models import Ambassador, AmbassadorEvent
from events.models import Event
from events.tests.base import EventsGraphQLTestCase
from recaps import inputs as recap_inputs
from recaps import models as recap_models
from recaps.mutations import RecapMutationService


@pytest.mark.django_db(transaction=True)
class TestMobileLegacyRecapSubmit(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.system_user = self.get_system_user()
        from tenants.models import Role
        from tenants.tests.base import ensure_role
        from utils.utils import ROLE_ID
        role = ensure_role(
            "Ambassador", slug=Role.AMBASSADOR_SLUG,
            pk=ROLE_ID.Ambassadors, created_by=self.system_user)
        self.tenant = self.create_tenant(name="Feel Free")
        etype = self.create_event_type(name="Field Sampling", tenant=self.tenant)
        status = self.create_event_status(name="Approved", tenant=self.tenant)
        self.event = Event.objects.create(
            name="Miami — Wynwood · 7/2", tenant=self.tenant,
            event_type=etype, status=status,
            address="13101 NE 16th Ave, Miami, FL 33161",
            created_by=self.system_user,
        )
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.ba_user = User.objects.create_user(
            username="alicia", email="aarchie00@gmail.com",
            first_name="Alicia", last_name="Archie", role=role, is_active=True,
        )
        self.amb = Ambassador.objects.create(
            user=self.ba_user, is_active=True,
            created_by=self.ba_user, updated_by=self.ba_user,
        )
        AmbassadorEvent.objects.create(
            ambassador=self.amb, event=self.event, tenant=self.tenant,
            is_approved=True, created_by=self.ba_user, updated_by=self.ba_user,
        )
        # Prod has FileType rows from other tenants (the create path falls
        # back to FileType.objects.first() globally) — mirror that.
        from recaps.models import FileType
        FileType.objects.create(name="Image", created_by=self.system_user)

    def test_mobile_shaped_input_with_event_uuid_creates_recap(self):
        # Exactly what RecapSubmitScreen sends (files/photos omitted).
        input_obj = recap_inputs.CreateRecapInput(
            event_id=str(self.event.uuid),
            name="7-Eleven recap",
            files=[recap_inputs.RecapFileInput(file="recaps/photos/test-blob.jpg")],
            products_sold=12,
            total_cans_sold=40,
            account_spend_amount=0.0,
            consumer_engagements=recap_inputs.ConsumerEngagementsInput(
                total_consumer=40,
                first_time_consumers=10,
                brand_aware_consumers=15,
                willing_to_purchase_consumers=12,
            ),
        )
        service = RecapMutationService.with_input(input_obj)
        service.user = self.ba_user
        recap = async_to_sync(service.create_recap)()
        assert recap.id is not None
        assert recap.event_id == self.event.id
        assert recap.event.tenant_id == self.tenant.id
        assert recap.recap_files.count() == 1
        assert recap_models.Recap.objects.filter(event=self.event).count() == 1
