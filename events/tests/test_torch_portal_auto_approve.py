"""Public createRequestByUrl auto-approves Torch only; others stay pending."""

from unittest.mock import MagicMock, patch

import pytest

from events import models as em
from events.envelopes import RequestorRequestApprovedMailer
from events.tests.base import EventsGraphQLTestCase
from events.torch_portal import TORCH_REQUEST_APPROVED_CC


CREATE_BY_URL = """
mutation CreateByUrl(
  $input: CreateRequestWithDependenciesInput!
  $requestUrlName: String!
) {
  createRequestByUrl(input: $input, requestUrlName: $requestUrlName) {
    success
    message
    request {
      uuid
      requestorEmail
      status { slug }
    }
  }
}
"""


def _public_input(*, request_type_id, timezone_id, extra=None):
    payload = {
        "name": "Whole Foods Market",
        "date": "2026-08-20T16:00:00+00:00",
        "startTime": "2026-08-20T16:00:00+00:00",
        "endTime": "2026-08-20T20:00:00+00:00",
        "address": "123 Main St, Austin, TX 78701",
        "coordinates": [0.0, 0.0],
        "timezoneId": str(timezone_id),
        "requestTypeId": str(request_type_id),
        "requestorEmail": "buyer@store.com",
        "clientName": "Jordan Buyer",
        "clientEmail": "buyer@store.com",
        "distributorName": "Republic",
        "distributorEmail": "dist@example.com",
        "retailerName": "Whole Foods Market",
        "retailerAddress": "123 Main St, Austin, TX 78701",
        "retailerStoreContact": "Store lead",
        "storeManagerName": "Alex",
        "storeManagerPhone": "5125550100",
        "details": [],
        "products": [],
    }
    if extra:
        payload.update(extra)
    return payload


@pytest.mark.django_db(transaction=True)
class TestTorchPortalAutoApprove(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.torch = self.create_tenant(
            name="Torch THC",
            slug="torch-thc",
            request_url_name="keee-torch-thc",
        )
        self.ld = self.create_tenant(
            name="Liquid Death",
            slug="liquid-death",
            request_url_name="ighn-liquid-death",
        )
        for tenant in (self.torch, self.ld):
            self.create_request_status(
                name="Pending", tenant=tenant, slug="pending", is_default=True
            )
            self.create_request_status(
                name="Approved", tenant=tenant, slug="approved", create_event=True
            )
            self.create_event_status(
                name="Pending", tenant=tenant, slug="pending", is_default=True
            )
            self.create_event_status(
                name="Approved", tenant=tenant, slug="approved"
            )
        self.torch_type = self.create_request_type(
            name="Retail Sampling", tenant=self.torch
        )
        self.ld_type = self.create_request_type(name="Sampling", tenant=self.ld)
        self.timezone = em.TimeZone.objects.create(
            name="UTC", code="UTC", offset=0, created_by=self.system_user
        )

    @pytest.mark.asyncio
    async def test_torch_public_form_creates_approved_request_and_event(self):
        mailer = MagicMock()
        with (
            patch(
                "events.mutations.RequestorRequestApprovedMailer",
                return_value=mailer,
            ) as MailerCls,
            patch("events.mutations.RequestorRequestCreatedMailer"),
            patch("events.mutations.RequestCreatedNotificationMailer"),
            patch("events.mutations.RmmAssignedRequestMailer"),
        ):
            result = await self._execute_mutation(
                CREATE_BY_URL,
                {
                    "input": _public_input(
                        request_type_id=self.torch_type.id,
                        timezone_id=self.timezone.id,
                        extra={"accountSpendAmount": "$500"},
                    ),
                    "requestUrlName": "keee-torch-thc",
                },
            )
        assert result.errors is None, result.errors
        payload = result.data["createRequestByUrl"]
        assert payload["success"] is True, payload["message"]
        assert payload["request"]["status"]["slug"] == "approved"
        assert payload["request"]["requestorEmail"] == "buyer@store.com"

        request_uuid = payload["request"]["uuid"]
        row = await em.Request.objects.select_related("status").aget(uuid=request_uuid)
        assert row.status.slug == "approved"
        event = await em.Event.objects.filter(request_id=row.id).afirst()
        assert event is not None, "Torch auto-approve must materialize an Event"

        kwargs = MailerCls.call_args.kwargs
        assert kwargs["to_emails"] == ["buyer@store.com"]
        cc_lower = {e.lower() for e in kwargs["cc_emails"]}
        for expected in TORCH_REQUEST_APPROVED_CC:
            assert expected.lower() in cc_lower
        assert kwargs["auto_approved"] is True
        mailer.send.assert_called()

    @pytest.mark.asyncio
    async def test_liquid_death_public_form_stays_pending(self):
        with (
            patch("events.mutations.RequestorRequestApprovedMailer.send") as approved,
            patch("events.mutations.RequestorRequestCreatedMailer.send"),
            patch("events.mutations.RequestCreatedNotificationMailer.send"),
            patch("events.mutations.RmmAssignedRequestMailer.send"),
        ):
            result = await self._execute_mutation(
                CREATE_BY_URL,
                {
                    "input": _public_input(
                        request_type_id=self.ld_type.id,
                        timezone_id=self.timezone.id,
                    ),
                    "requestUrlName": "ighn-liquid-death",
                },
            )
        assert result.errors is None, result.errors
        payload = result.data["createRequestByUrl"]
        assert payload["success"] is True, payload["message"]
        assert payload["request"]["status"]["slug"] == "pending"
        approved.assert_not_called()
        event = await em.Event.objects.filter(
            request__uuid=payload["request"]["uuid"]
        ).afirst()
        assert event is None

    @pytest.mark.asyncio
    async def test_torch_url_against_other_tenant_stays_pending(self):
        """Tampered tenant_id on the Torch URL must not auto-approve LD."""
        with (
            patch("events.mutations.RequestorRequestApprovedMailer.send") as approved,
            patch("events.mutations.RequestorRequestCreatedMailer.send"),
            patch("events.mutations.RequestCreatedNotificationMailer.send"),
            patch("events.mutations.RmmAssignedRequestMailer.send"),
        ):
            result = await self._execute_mutation(
                CREATE_BY_URL,
                {
                    "input": _public_input(
                        request_type_id=self.ld_type.id,
                        timezone_id=self.timezone.id,
                        extra={"tenantId": str(self.ld.id)},
                    ),
                    "requestUrlName": "keee-torch-thc",
                },
            )
        assert result.errors is None, result.errors
        payload = result.data["createRequestByUrl"]
        assert payload["success"] is True, payload["message"]
        assert payload["request"]["status"]["slug"] == "pending"
        approved.assert_not_called()


@pytest.mark.django_db(transaction=True)
class TestTorchApprovedMailerFields(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.request_type = self.create_request_type(
            name="On-Premise", tenant=self.tenant
        )

    def test_envelope_includes_program_fields_when_filled(self):
        req = em.Request.objects.create(
            name="Torch bar",
            address="1 A St, Austin, TX",
            tenant=self.tenant,
            request_type=self.request_type,
            requestor_email="buyer@store.com",
            client_name="Jordan Buyer",
            account_spend_amount="$1,200",
            event_assets_needed="10x10, Barrel Cooler",
            load_in_time="8:00 AM",
            onsite_poc="Liberty",
            additional_team_details="Park in the rear",
            created_by=self.system_user,
        )
        env = RequestorRequestApprovedMailer(
            request=req,
            location=None,
            to_emails=["buyer@store.com"],
            cc_emails=["liberty@torchdrinks.com"],
            auto_approved=True,
        ).envelope()
        assert env.subject == "Your activation request is approved — Spark by Ignite"
        assert env.context["auto_approved"] is True
        assert env.context["account_spend_amount"] == "$1,200"
        assert env.context["event_assets_needed"] == "10x10, Barrel Cooler"
        assert env.context["load_in_time"] == "8:00 AM"
        assert env.context["onsite_poc"] == "Liberty"
        assert env.context["additional_team_details"] == "Park in the rear"
        assert env.context["activation_type"] == "On-Premise"
