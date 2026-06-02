"""
Tests for task #253: save a Job's current briefing AS a reusable template.

The rest of the briefing-template feature already exists; the only missing
piece was snapshotting THIS job's briefing into a new BriefingTemplate. The new
mutation reads job.briefing_title / briefing_body, creates a BriefingTemplate
under job.tenant_id, and clones job.briefing_attachments.
"""
import uuid

import pytest
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from strawberry.relay import to_base64

from jobs import models
from jobs.tests.base import JobsGraphQLTestCase

User = get_user_model()


SAVE_MUTATION = """
    mutation Save($input: SaveJobBriefingAsTemplateInput!) {
        saveJobBriefingAsTemplate(input: $input) {
            success
            message
            briefingTemplate {
                uuid
                name
                title
                body
                tenantId
                attachments { name url }
            }
        }
    }
"""

TEMPLATES_QUERY = """
    query Templates($tenantId: ID) {
        briefingTemplates(tenantId: $tenantId) {
            uuid
            name
            title
        }
    }
"""


@pytest.mark.django_db(transaction=True)
class TestSaveJobBriefingAsTemplate(JobsGraphQLTestCase):

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_spark import schema_spark

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Briefing Co")
        uid = str(uuid.uuid4())[:8]
        self.admin_user = self.create_user(
            username=f"admin_{uid}@test.com",
            email=f"admin_{uid}@test.com",
            role=self.roles["spark_admin"],
            password="testpass123",
            is_staff=True,
            is_superuser=True,
        )
        self.create_tenanted_user(user=self.admin_user, tenant=self.tenant)

        self.event = self.create_event(
            name="Briefing Event", tenant=self.tenant, address="3 Brief St"
        )
        self.job_title = self.create_job_title(name="BA", tenant=self.tenant)
        self.job = self.create_job(
            name="Briefed Gig",
            code=f"GIG-{uid}",
            address="3 Brief St",
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            briefing_title="Arrive 15 min early",
            briefing_body="Wear the branded tee. Set up the sampling table by the door.",
        )
        # A briefing attachment on the job that should be cloned.
        models.JobBriefingAttachment.objects.create(
            job=self.job,
            name="run-of-show.pdf",
            url="briefings/job/run-of-show.pdf",
            content_type="application/pdf",
            size=2048,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_save_creates_template_with_job_briefing_and_clones_attachments(self):
        result = await self._execute_mutation_authenticated(
            SAVE_MUTATION,
            {
                "input": {
                    "jobId": to_base64("Job", self.job.id),
                    "name": "Standard sampling briefing",
                }
            },
            self.admin_user,
            self.endpoint_path,
        )
        assert result.errors is None, f"errors: {result.errors}"
        payload = result.data["saveJobBriefingAsTemplate"]
        assert payload["success"] is True
        tpl = payload["briefingTemplate"]
        assert tpl["name"] == "Standard sampling briefing"
        assert tpl["title"] == "Arrive 15 min early"
        assert tpl["body"].startswith("Wear the branded tee")
        # cloned attachment carries over.
        assert len(tpl["attachments"]) == 1
        assert tpl["attachments"][0]["name"] == "run-of-show.pdf"
        assert tpl["attachments"][0]["url"] == "briefings/job/run-of-show.pdf"

        # Persisted under the job's tenant.
        tpl_row = await sync_to_async(
            lambda: models.BriefingTemplate.objects.get(uuid=tpl["uuid"])
        )()
        assert await sync_to_async(lambda: tpl_row.tenant_id)() == self.tenant.id
        att_count = await sync_to_async(
            lambda: tpl_row.attachments.count()
        )()
        assert att_count == 1

    @pytest.mark.asyncio
    async def test_saved_template_then_appears_in_briefing_templates(self):
        save = await self._execute_mutation_authenticated(
            SAVE_MUTATION,
            {"input": {"jobId": to_base64("Job", self.job.id), "name": "Listed tpl"}},
            self.admin_user,
            self.endpoint_path,
        )
        assert save.data["saveJobBriefingAsTemplate"]["success"] is True

        listing = await self._execute_query_authenticated(
            TEMPLATES_QUERY, {"tenantId": str(self.tenant.id)}, self.admin_user,
            self.endpoint_path,
        )
        assert listing.errors is None, f"errors: {listing.errors}"
        names = [t["name"] for t in listing.data["briefingTemplates"]]
        assert "Listed tpl" in names
