from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
import strawberry_django  # noqa: F401

from ambassadors.models import AmbassadorEvent
from events.models import EventType
from jobs.tests.base import JobsGraphQLTestCase
from recaps import models as recap_models
from recaps.envelopes import RecapApprovedNotificationMailer


@pytest.mark.django_db(transaction=True)
class TestApproveRecapNotifications(JobsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Recap Tenant")

        self.spark_user = self.create_user(
            username="spark_recap@test.com",
            email="spark_recap@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.spark_user, tenant=self.tenant)

        self.rmm_user = self.create_user(
            username="rmm_recap@test.com",
            email="rmm_recap@test.com",
            first_name="Rosa",
            role=self.roles["client"],
            password="testpass123",
        )
        self.create_tenanted_user(user=self.rmm_user, tenant=self.tenant)

        self.event = self.create_event(
            name="Recap Event",
            tenant=self.tenant,
            address="123 Recap St",
            rmm_asigned=self.rmm_user,
        )
        self.job_title = self.create_job_title(name="BA", tenant=self.tenant)
        self.job = self.create_job(
            name="Recap Job",
            code="RECAP-JOB-001",
            address="456 Activation Ave",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
        )

        self.ambassador_user = self.create_user(
            username="recap_amb@test.com",
            email="recap_amb@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.ambassador = self.create_ambassador(user=self.ambassador_user)
        self.create_tenanted_user(user=self.ambassador_user, tenant=self.tenant)

        self.other_ambassador_user = self.create_user(
            username="other_recap_amb@test.com",
            email="other_recap_amb@test.com",
            role=self.roles["ambassador"],
            password="testpass123",
        )
        self.other_ambassador = self.create_ambassador(user=self.other_ambassador_user)
        self.create_tenanted_user(user=self.other_ambassador_user, tenant=self.tenant)

        system_user = self.get_system_user()
        AmbassadorEvent.objects.create(
            ambassador=self.ambassador,
            event=self.event,
            tenant=self.tenant,
            created_by=system_user,
        )
        AmbassadorEvent.objects.create(
            ambassador=self.other_ambassador,
            event=self.event,
            tenant=self.tenant,
            created_by=system_user,
        )

        self.recap = recap_models.Recap.objects.create(
            name="Post activation recap",
            approved=False,
            event=self.event,
            job=self.job,
            ambassador=self.ambassador,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_approve_recap_sends_notification_email(self):
        mutation = """
        mutation ApproveRecap($input: ApproveRecapInput!) {
            approveRecap(input: $input) {
                success
                message
                recap {
                    id
                    approved
                }
            }
        }
        """
        variables = {
            "input": {
                "id": str(self.recap.id),
                "approved": True,
            }
        }

        with patch("recaps.mutations.RecapApprovedNotificationMailer.send") as mock_send:
            result = await self._execute_mutation_authenticated(
                mutation,
                variables,
                self.spark_user,
                self.endpoint_path,
            )

        assert result.data is not None
        assert result.data["approveRecap"]["success"] is True
        assert result.data["approveRecap"]["recap"]["approved"] is True
        assert mock_send.called

    @pytest.mark.asyncio
    async def test_recap_approved_mailer_template_renders(self):
        self.recap.approved = True
        await sync_to_async(self.recap.save)()
        recap = await sync_to_async(
            recap_models.Recap.objects.select_related(
                "event",
                "event__tenant",
                "job",
                "retailer",
                "timezone",
                "ambassador",
            ).get
        )(id=self.recap.id)

        mailer = RecapApprovedNotificationMailer(
            recap=recap,
            to_emails=[self.rmm_user.email],
            recipient_first_name=self.rmm_user.first_name,
            reply_to_email=self.rmm_user.email,
        )
        envelope = mailer.envelope()
        rendered_html = envelope.render_template()

        assert envelope.template == "recaps.templates.emails.recap_approved_notification"
        assert envelope.to_emails == [self.rmm_user.email]
        assert "Activation Summary" in rendered_html

    @pytest.mark.asyncio
    async def test_recap_query_returns_only_assigned_ambassador(self):
        query = """
        query Recap($uuid: ID!) {
            recap(uuid: $uuid) {
                id
                ambassador {
                    id
                }
            }
        }
        """
        variables = {"uuid": str(self.recap.uuid)}

        result = await self._execute_query_authenticated(
            query,
            variables,
            self.spark_user,
            self.endpoint_path,
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["recap"]["id"] == str(self.recap.id)
        assert result.data["recap"]["ambassador"] == {"id": str(self.ambassador.id)}

    @pytest.mark.asyncio
    async def test_recaps_query_filters_by_ambassador(self):
        other_recap = recap_models.Recap.objects.create(
            name="Other ambassador recap",
            approved=False,
            event=self.event,
            job=self.job,
            ambassador=self.other_ambassador,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )

        query = """
        query Recaps($filters: RecapFiltersInput) {
            recaps(filters: $filters) {
                totalCount
                edges {
                    node {
                        id
                        ambassador {
                            id
                        }
                    }
                }
            }
        }
        """
        variables = {"filters": {"ambassadorId": str(self.ambassador.id)}}

        result = await self._execute_query_authenticated(
            query,
            variables,
            self.spark_user,
            self.endpoint_path,
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["recaps"]["totalCount"] == 1
        assert result.data["recaps"]["edges"] == [
            {
                "node": {
                    "id": str(self.recap.id),
                    "ambassador": {"id": str(self.ambassador.id)},
                }
            }
        ]
        assert str(other_recap.id) not in {
            edge["node"]["id"] for edge in result.data["recaps"]["edges"]
        }

    @pytest.mark.asyncio
    async def test_recaps_query_filters_by_approved(self):
        approved_recap = recap_models.Recap.objects.create(
            name="Approved recap",
            approved=True,
            event=self.event,
            job=self.job,
            ambassador=self.other_ambassador,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )

        query = """
        query Recaps($filters: RecapFiltersInput) {
            recaps(filters: $filters) {
                totalCount
                edges {
                    node {
                        id
                        approved
                    }
                }
            }
        }
        """
        variables = {"filters": {"approved": True}}

        result = await self._execute_query_authenticated(
            query,
            variables,
            self.spark_user,
            self.endpoint_path,
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["recaps"]["totalCount"] == 1
        assert result.data["recaps"]["edges"] == [
            {
                "node": {
                    "id": str(approved_recap.id),
                    "approved": True,
                }
            }
        ]

    @pytest.mark.asyncio
    async def test_update_custom_recap_updates_existing_and_creates_new_field_values(self):
        @sync_to_async
        def create_custom_recap_data():
            system_user = self.get_system_user()
            event_type = EventType.objects.create(
                name="Sampling",
                slug="sampling",
                tenant=self.tenant,
                created_by=system_user,
            )
            template = recap_models.CustomRecapTemplate.objects.create(
                name="Sampling recap",
                event_type=event_type,
                tenant=self.tenant,
                created_by=system_user,
            )
            section = recap_models.RecapSection.objects.create(
                name="Main",
                tenant=self.tenant,
                created_by=system_user,
            )
            field_type = recap_models.CustomRecapFieldType.objects.create(
                name="Text",
                created_by=system_user,
            )
            existing_field = recap_models.CustomField.objects.create(
                name="Existing field",
                custom_recap_template=template,
                custom_field_type=field_type,
                recap_section=section,
                created_by=system_user,
            )
            new_field = recap_models.CustomField.objects.create(
                name="New field",
                custom_recap_template=template,
                custom_field_type=field_type,
                recap_section=section,
                created_by=system_user,
            )
            removed_field = recap_models.CustomField.objects.create(
                name="Removed field",
                custom_recap_template=template,
                custom_field_type=field_type,
                recap_section=section,
                created_by=system_user,
            )
            custom_recap = recap_models.CustomRecap.objects.create(
                name="Original custom recap",
                event=self.event,
                tenant=self.tenant,
                custom_recap_template=template,
                created_by=self.spark_user,
            )
            existing_value = recap_models.CustomFieldValue.objects.create(
                custom_recap=custom_recap,
                custom_field=existing_field,
                value="old value",
                created_by=self.spark_user,
            )
            removed_value = recap_models.CustomFieldValue.objects.create(
                custom_recap=custom_recap,
                custom_field=removed_field,
                value="remove me",
                created_by=self.spark_user,
            )

            return {
                "template_id": template.id,
                "custom_recap_id": custom_recap.id,
                "existing_value_id": existing_value.id,
                "existing_field_id": existing_field.id,
                "new_field_id": new_field.id,
                "removed_value_id": removed_value.id,
            }

        ids = await create_custom_recap_data()

        mutation = """
        mutation UpdateCustomRecap($input: UpdateCustomRecapInput!) {
            updateCustomRecap(input: $input) {
                success
                message
                customRecap {
                    id
                }
            }
        }
        """
        variables = {
            "input": {
                "id": str(ids["custom_recap_id"]),
                "name": "Updated custom recap",
                "eventId": str(self.event.id),
                "customRecapTemplateId": str(ids["template_id"]),
                "customFieldValues": [
                    {
                        "customFieldValueId": str(ids["existing_value_id"]),
                        "value": "updated value",
                    },
                    {
                        "customFieldId": str(ids["new_field_id"]),
                        "value": "new value",
                    },
                ],
            }
        }

        result = await self._execute_mutation_authenticated(
            mutation,
            variables,
            self.spark_user,
            self.endpoint_path,
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["updateCustomRecap"]["success"] is True

        @sync_to_async
        def get_field_values():
            return list(
                recap_models.CustomFieldValue.objects.filter(
                    custom_recap_id=ids["custom_recap_id"],
                )
                .order_by("custom_field_id")
                .values("id", "custom_field_id", "value", "updated_by_id")
            )

        values = await get_field_values()

        assert {
            (item["custom_field_id"], item["value"]) for item in values
        } == {
            (ids["existing_field_id"], "updated value"),
            (ids["new_field_id"], "new value"),
        }
        assert any(
            item["id"] == ids["existing_value_id"]
            and item["updated_by_id"] == self.spark_user.id
            for item in values
        )
        assert ids["removed_value_id"] not in {item["id"] for item in values}
