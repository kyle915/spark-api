"""
Tests for apply_ambassador_event mutation.

This module tests:
- apply_ambassador_event (authenticated mutation)
"""
import pytest
import strawberry_django  # noqa: F401
import uuid
from asgiref.sync import sync_to_async
from events.models import Event, Request, RequestStatus, RequestType
from ambassadors.models import Ambassador, AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_spark import schema_spark


@pytest.mark.django_db(transaction=True)
class TestApplyAmbassadorEvent(AmbassadorsGraphQLTestCase):
    """Tests for apply_ambassador_event mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"
        self.system_user = self.get_system_user()

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Apply Event Test Tenant")
        # Use UUID for user to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.user = self.create_user(
            username=f"ambassador_apply_{unique_id}@spark.local",
            email=f"ambassador_apply_{unique_id}@spark.local",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.user, self.tenant)

        # Create Ambassador profile
        self.ambassador = self.create_ambassador(
            user=self.user,
            created_by=self.system_user,
        )

        self.request_status = self.create_request_status("Pending", self.tenant)
        self.request_type = self.create_request_type("Demo", self.tenant)

        # Create Request manually (helper requires client/distributor/retailer we don't need)
        self.request = Request.objects.create(
            name="Test Request",
            date="2023-10-27",
            address="123 Test St",
            tenant=self.tenant,
            status=self.request_status,
            request_type=self.request_type,
            created_by=self.system_user,
        )

        # Create Event manually
        self.event = Event.objects.create(
            name="Test Event",
            tenant=self.tenant,
            request=self.request,
            created_by=self.system_user,
            address="123 Test St"
        )

        self.mutation = """
            mutation ApplyAmbassadorEvent($eventId: ID!) {
                applyAmbassadorEvent(eventId: $eventId) {
                    success
                    message
                    application {
                        id
                        isApproved
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_apply_ambassador_event_success(self):
        """Test successful ambassador event application."""
        variables = {"eventId": str(self.event.id)}
        response = await self._execute_mutation_authenticated(
            self.mutation, variables=variables, user=self.user, endpoint_path=self.endpoint_path
        )

        assert response.errors is None
        assert response.data["applyAmbassadorEvent"]["success"] is True
        assert response.data["applyAmbassadorEvent"]["message"] == "Application successful"
        assert response.data["applyAmbassadorEvent"]["application"]["id"] is not None
        assert response.data["applyAmbassadorEvent"]["application"]["isApproved"] is False

        # Verify DB
        exists = await sync_to_async(
            AmbassadorEvent.objects.filter(
                ambassador=self.ambassador, event=self.event
            ).exists
        )()
        assert exists is True

    @pytest.mark.asyncio
    async def test_apply_ambassador_event_already_applied(self):
        """Test applying to an event that was already applied to."""
        # Create existing application
        await sync_to_async(AmbassadorEvent.objects.create)(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            created_by=self.user,
        )

        variables = {"eventId": str(self.event.id)}
        response = await self._execute_mutation_authenticated(
            self.mutation, variables=variables, user=self.user, endpoint_path=self.endpoint_path
        )

        assert response.data["applyAmbassadorEvent"]["success"] is False
        assert response.data["applyAmbassadorEvent"]["message"] == "Already applied to this event"

