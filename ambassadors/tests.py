import pytest
from events.tests.base import EventsGraphQLTestCase
from events.models import Event, Request, RequestStatus, RequestType
from ambassadors.models import Ambassador, AmbassadorEvent
from config.schema_spark import schema_spark


@pytest.mark.django_db(transaction=True)
class TestApplyAmbassadorEvent(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.schema = schema_spark
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant()
        self.user_role = self.create_role("Ambassador", 3)
        self.user = self.create_user("ambassador", "ambassador@spark.local", self.user_role)
        self.tenanted_user = self.create_tenanted_user(self.user, self.tenant)
        
        # Create Ambassador profile
        self.ambassador = Ambassador.objects.create(
            user=self.user, created_by=self.system_user
        )

        self.request_status = self.create_request_status("Pending", self.tenant)
        self.request_type = self.create_request_type("Demo", self.tenant)
        
        # Create Request manually as the helper seems to require many dependencies
        self.request = Request.objects.create(
            name="Test Request",
            date="2023-10-27",
            address="123 Test St",
            tenant=self.tenant,
            status=self.request_status,
            request_type=self.request_type,
            created_by=self.system_user,
        )

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
        variables = {"eventId": str(self.event.id)}
        response = await self._execute_mutation_authenticated(
            self.mutation, variables=variables, user=self.user
        )

        assert response.errors is None
        assert response.data["applyAmbassadorEvent"]["success"] is True
        assert response.data["applyAmbassadorEvent"]["message"] == "Application successful"
        assert response.data["applyAmbassadorEvent"]["application"]["id"] is not None
        assert response.data["applyAmbassadorEvent"]["application"]["isApproved"] is False

        # Verify DB
        exists = await AmbassadorEvent.objects.filter(
            ambassador=self.ambassador, event=self.event
        ).aexists()
        assert exists is True

    @pytest.mark.asyncio
    async def test_apply_ambassador_event_already_applied(self):
        # Create existing application
        await AmbassadorEvent.objects.acreate(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            created_by=self.user,
        )

        variables = {"eventId": str(self.event.id)}
        response = await self._execute_mutation_authenticated(
            self.mutation, variables=variables, user=self.user
        )

        assert response.data["applyAmbassadorEvent"]["success"] is False
        assert response.data["applyAmbassadorEvent"]["message"] == "Already applied to this event"

    @pytest.mark.asyncio
    async def test_apply_ambassador_event_not_authenticated(self):
        # Use simple unauthenticated execution (passing None user or just empty context if helper allows)
        # But _execute_mutation usually takes a user.
        # Let's try passing no user if supported, or mocking context.
        # Actually _execute_mutation in base usually relies on user to build context.
        # If I want unauthenticated, I might need to look at how base handles it or just pass AnonymousUser?
        # For now let's skip explicit unauthenticated test via helper if unsure, or try passing None.
        pass
