"""Per-tenant request auto-approve (Tenant.auto_approve_requests).

FAMUS / Alcohol Armor / Wook Water: public spark-form and client-portal
requests land approved with an Event, and the approval email goes to the
requestor with the brand's CC list (no whole-Ignite roll-up, and the CC list
is dropped from the duplicate "New client submission" alert). Tenants without
the flag keep their existing flow.
"""

import io
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from events import models as em
from events.request_auto_approve import auto_approved_request_lists
from events.tests.base import EventsGraphQLTestCase
from tenants.models import Tenant, TenantTheme

BRAND_CC = [
    "kyle@igniteproductions.co",
    "baesha@igniteproductions.co",
    "keis@igniteproductions.co",
    "nevena@igniteproductions.co",
    "myriant@igniteproductions.co",
]

CREATE_BY_URL = """
mutation CreateByUrl(
  $input: CreateRequestWithDependenciesInput!
  $requestUrlName: String!
) {
  createRequestByUrl(input: $input, requestUrlName: $requestUrlName) {
    success
    message
    request { uuid status { slug } }
  }
}
"""

CREATE_REQUEST = """
mutation CreateRequest($input: CreateRequestInput!) {
  createRequest(input: $input) {
    success
    message
    request { uuid status { slug } }
  }
}
"""


def _input(*, request_type_id, timezone_id, extra=None):
    payload = {
        "name": "Publix #1201",
        "date": "2026-11-20T16:00:00+00:00",
        "startTime": "2026-11-20T16:00:00+00:00",
        "endTime": "2026-11-20T20:00:00+00:00",
        "address": "100 Main St, Charlotte, NC 28202",
        "coordinates": [0.0, 0.0],
        "timezoneId": str(timezone_id),
        "requestTypeId": str(request_type_id),
        "details": [],
        "products": [],
    }
    if extra:
        payload.update(extra)
    return payload


def _public_extra():
    return {
        "requestorEmail": "buyer@store.com",
        "clientName": "Jordan Buyer",
        "clientEmail": "buyer@store.com",
        "retailerName": "Publix",
        "retailerAddress": "100 Main St, Charlotte, NC 28202",
        "retailerStoreContact": "Store lead",
        "distributorName": "",
        "distributorEmail": "",
        "storeManagerName": "Alex",
        "storeManagerPhone": "7045550100",
    }


def test_lists_dedupe_requestor_and_require_flag():
    tenant = Tenant(
        auto_approve_requests=True,
        request_approved_cc_emails="kyle@igniteproductions.co, keis@igniteproductions.co",
    )
    assert auto_approved_request_lists(tenant, "Kyle@igniteproductions.co") == (
        ["Kyle@igniteproductions.co"],
        ["keis@igniteproductions.co"],
    )
    assert auto_approved_request_lists(tenant, "") == (
        ["kyle@igniteproductions.co"],
        ["keis@igniteproductions.co"],
    )
    tenant.auto_approve_requests = False
    assert auto_approved_request_lists(tenant, "buyer@store.com") == (
        ["buyer@store.com"],
        [],
    )


@pytest.mark.django_db(transaction=True)
class TestRequestAutoApprove(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.famus = self.create_tenant(
            name="FAMUS Energy",
            slug="famus-energy",
            request_url_name="famus-energy",
            auto_approve_requests=True,
            request_approved_cc_emails=", ".join(BRAND_CC),
        )
        self.wook = self.create_tenant(
            name="Wook Water",
            slug="wook-water",
            request_url_name="wook-water",
            auto_approve_requests=True,
            request_approved_cc_emails=", ".join(BRAND_CC),
        )
        self.other = self.create_tenant(
            name="Other Brand", slug="other-brand", request_url_name="other-brand"
        )
        group = self._brand_group()
        Tenant.objects.filter(id__in=[self.famus.id, self.wook.id]).update(
            brand_group=group
        )
        TenantTheme.objects.create(
            tenant=self.famus,
            color_scheme="light",
            css_variables={"--color-primary": "#C8A058"},
        )
        self.types = {}
        for tenant in (self.famus, self.wook, self.other):
            self.create_request_status(
                name="Pending", tenant=tenant, slug="pending", is_default=True
            )
            self.create_request_status(
                name="Approved", tenant=tenant, slug="approved", create_event=True
            )
            self.create_event_status(
                name="Pending", tenant=tenant, slug="pending", is_default=True
            )
            self.create_event_status(name="Approved", tenant=tenant, slug="approved")
            self.create_event_type(name="Retail Sampling", tenant=tenant)
            self.types[tenant.id] = self.create_request_type(
                name="Retail Sampling", tenant=tenant
            )
        product_type = em.ProductType.objects.create(
            tenant=self.wook, name="Hydration", created_by=self.system_user
        )
        self.wook_sku = em.Product.objects.create(
            tenant=self.wook,
            name="Laser Lime 12oz Can",
            product_type=product_type,
            created_by=self.system_user,
        )
        self.timezone = em.TimeZone.objects.create(
            name="UTC", code="UTC", offset=0, created_by=self.system_user
        )

    def _brand_group(self):
        from tenants.models import BrandGroup

        return BrandGroup.objects.create(name="Test group", slug="test-group")

    def _client_user(self, tenant, email):
        user = self.create_user(username=email, email=email, role=self.roles["client"])
        self.create_tenanted_user(user=user, tenant=tenant)
        return user

    async def _public(self, tenant, extra=None):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        return await self._execute_mutation(
            CREATE_BY_URL,
            {
                "input": _input(
                    request_type_id=self.types[tenant.id].id,
                    timezone_id=self.timezone.id,
                    extra={**_public_extra(), **(extra or {})},
                ),
                "requestUrlName": tenant.request_url_name,
            },
        )

    async def _logged_in(self, tenant, user):
        from config.schema_spark import schema_spark

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql"
        return await self._execute_mutation_authenticated(
            CREATE_REQUEST,
            {
                "input": _input(
                    request_type_id=self.types[tenant.id].id,
                    timezone_id=self.timezone.id,
                )
            },
            user=user,
        )

    @staticmethod
    def _payload(result, key):
        assert result.errors is None, result.errors
        payload = result.data[key]
        assert payload["success"] is True, payload["message"]
        return payload

    @staticmethod
    async def _event_for(uuid):
        return (
            await em.Event.objects.select_related("status")
            .filter(request__uuid=uuid)
            .afirst()
        )

    @pytest.mark.asyncio
    async def test_public_form_auto_approves_and_mails_requestor_plus_brand_cc(self):
        mailer = MagicMock()
        with (
            patch(
                "events.mutations.RequestorRequestApprovedMailer", return_value=mailer
            ) as approved_cls,
            patch("events.mutations.RequestorRequestCreatedMailer") as created_cls,
            patch("events.mutations.RequestCreatedNotificationMailer") as group_cls,
            patch("events.mutations.RmmAssignedRequestMailer") as rmm_cls,
            patch("events.mutations.ClientRequestCreatedNotificationMailer") as alert_cls,
            patch("events.mutations.append_torch_public_form_row", return_value=False),
        ):
            result = await self._public(self.famus)
        payload = self._payload(result, "createRequestByUrl")
        assert payload["request"]["status"]["slug"] == "approved"

        event = await self._event_for(payload["request"]["uuid"])
        assert event is not None, "auto-approve must materialize the Event"
        assert event.status.slug == "approved"
        log = await em.RequestActivityLog.objects.filter(
            request__uuid=payload["request"]["uuid"],
            kind=em.RequestActivityLog.KIND_STATUS_CHANGED,
        ).afirst()
        assert log is not None and "auto-approved" in log.summary

        approved_cls.assert_called_once()
        kwargs = approved_cls.call_args.kwargs
        assert kwargs["to_emails"] == ["buyer@store.com"]
        assert kwargs["cc_emails"] == BRAND_CC
        assert kwargs["auto_approved"] is True
        assert kwargs["brand_color"] == "#C8A058"
        assert kwargs["request_again_url"].endswith("/spark-form/famus-energy")
        assert kwargs["request_again_url"].startswith("https://client.igniteproductions.co")
        mailer.send.assert_called_once()
        for cls in (created_cls, group_cls, rmm_cls, alert_cls):
            cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_public_multi_brand_request_approves_host_once(self):
        with (
            patch("events.mutations.RequestorRequestApprovedMailer") as approved_cls,
            patch("events.mutations.RequestorRequestCreatedMailer"),
            patch("events.mutations.RequestCreatedNotificationMailer"),
            patch("events.mutations.RmmAssignedRequestMailer"),
            patch("events.mutations.append_torch_public_form_row", return_value=False),
        ):
            result = await self._public(
                self.famus,
                extra={
                    "coBrands": [
                        {
                            "tenantId": str(self.wook.id),
                            "productIds": [str(self.wook_sku.id)],
                        }
                    ]
                },
            )
        payload = self._payload(result, "createRequestByUrl")
        assert payload["request"]["status"]["slug"] == "approved"
        request = await em.Request.objects.aget(uuid=payload["request"]["uuid"])
        assert await em.RequestCoBrand.objects.filter(
            request=request, tenant=self.wook
        ).aexists()
        assert await em.Event.objects.filter(request=request).acount() == 1
        assert approved_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_public_form_without_flag_stays_pending(self):
        with (
            patch("events.mutations.RequestorRequestApprovedMailer") as approved_cls,
            patch("events.mutations.RequestorRequestCreatedMailer") as created_cls,
            patch("events.mutations.RequestCreatedNotificationMailer"),
            patch("events.mutations.RmmAssignedRequestMailer"),
            patch("events.mutations.append_torch_public_form_row", return_value=False),
        ):
            result = await self._public(self.other)
        payload = self._payload(result, "createRequestByUrl")
        assert payload["request"]["status"]["slug"] == "pending"
        assert await self._event_for(payload["request"]["uuid"]) is None
        approved_cls.assert_not_called()
        created_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_client_login_mails_brand_cc_and_drops_it_from_alert(self):
        user = await self._acreate_client("buyer@famus.com", self.famus)
        with (
            patch("events.mutations.RequestorRequestApprovedMailer") as approved_cls,
            patch("events.mutations.ClientRequestCreatedNotificationMailer") as alert_cls,
        ):
            result = await self._logged_in(self.famus, user)
        payload = self._payload(result, "createRequest")
        assert payload["request"]["status"]["slug"] == "approved"
        assert await self._event_for(payload["request"]["uuid"]) is not None

        kwargs = approved_cls.call_args.kwargs
        assert kwargs["to_emails"] == ["buyer@famus.com"]
        assert kwargs["cc_emails"] == BRAND_CC
        alert_to = {e.lower() for e in alert_cls.call_args.kwargs["to_emails"]}
        assert not alert_to & set(BRAND_CC)
        assert "events@igniteproductions.co" in alert_to

    @pytest.mark.asyncio
    async def test_client_login_without_flag_keeps_ignite_roll_up(self):
        user = await self._acreate_client("buyer@other.com", self.other)
        with (
            patch("events.mutations.RequestorRequestApprovedMailer") as approved_cls,
            patch("events.mutations.ClientRequestCreatedNotificationMailer") as alert_cls,
        ):
            result = await self._logged_in(self.other, user)
        self._payload(result, "createRequest")
        kwargs = approved_cls.call_args.kwargs
        assert kwargs["to_emails"] == ["buyer@other.com"]
        assert "events@igniteproductions.co" in {e.lower() for e in kwargs["cc_emails"]}
        assert "brand_color" not in kwargs
        alert_to = {e.lower() for e in alert_cls.call_args.kwargs["to_emails"]}
        assert "kyle@igniteproductions.co" in alert_to

    @pytest.mark.asyncio
    async def test_admin_create_request_always_sends_confirmation_and_copies_details(
        self,
    ):
        from asgiref.sync import sync_to_async
        from config.schema_spark import schema_spark

        admin = await sync_to_async(
            lambda: self.create_user(
                username="ops@igniteproductions.co",
                email="ops@igniteproductions.co",
                role=self.roles["spark_admin"],
            )
        )()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql"
        with patch("events.mutations.RequestorRequestApprovedMailer") as approved_cls:
            result = await self._execute_mutation_authenticated(
                CREATE_REQUEST,
                {
                    "input": _input(
                        request_type_id=self.types[self.famus.id].id,
                        timezone_id=self.timezone.id,
                        extra={
                            "tenantId": str(self.famus.id),
                            "name": "american fuel · 2026-10-16",
                            "notes": "Morning commuter rush\nBA count: 1",
                        },
                    )
                },
                user=admin,
            )
        payload = self._payload(result, "createRequest")
        assert payload["request"]["status"]["slug"] == "approved"
        approved_cls.assert_called_once()
        kwargs = approved_cls.call_args.kwargs
        assert kwargs["to_emails"] == ["ops@igniteproductions.co"]
        assert kwargs["cc_emails"] == BRAND_CC

        req_row = await em.Request.objects.aget(uuid=payload["request"]["uuid"])
        assert req_row.retailer_name == "american fuel"
        assert req_row.retailer_address == "100 Main St, Charlotte, NC 28202"

        event = await self._event_for(payload["request"]["uuid"])
        assert event is not None
        assert event.notes == "Morning commuter rush\nBA count: 1"

    async def _acreate_client(self, email, tenant):
        from asgiref.sync import sync_to_async

        return await sync_to_async(self._client_user)(tenant, email)


@pytest.mark.django_db(transaction=True)
class TestSetRequestAutoApproveCommand(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tenant = self.create_tenant(
            name="Alcohol Armor", slug="alcohol-armor", request_url_name="alcohol-armor"
        )
        self.lookalike = self.create_tenant(
            name="Alcohol Armor Old", slug="alcohol-armor-old", request_url_name="aa-old"
        )

    def _run(self, **opts):
        out = io.StringIO()
        call_command("set_request_auto_approve", stdout=out, **opts)
        return out.getvalue()

    def test_dry_run_then_apply(self):
        log = self._run(tenants="alcohol-armor", cc=",".join(BRAND_CC))
        assert "DRY-RUN" in log
        self.tenant.refresh_from_db()
        assert self.tenant.auto_approve_requests is False

        self._run(tenants="alcohol-armor", cc=",".join(BRAND_CC), apply=True)
        self.tenant.refresh_from_db()
        assert self.tenant.auto_approve_requests is True
        assert self.tenant.request_approved_cc_emails == ", ".join(BRAND_CC)
        self.lookalike.refresh_from_db()
        assert self.lookalike.auto_approve_requests is False

        self._run(tenants="alcohol-armor", off=True, apply=True)
        self.tenant.refresh_from_db()
        assert self.tenant.auto_approve_requests is False
        assert self.tenant.request_approved_cc_emails == ", ".join(BRAND_CC)
