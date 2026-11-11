"""Photo gallery, per-hour efficiency and cost-per-sample."""

from __future__ import annotations

from datetime import datetime

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from events import models as event_models
from events.campaign_pnl import _per
from recaps import models as recap_models
from recaps.field_efficiency import daypart_for_hour, summarize_efficiency


# ─── Pure roll-up ───────────────────────────────────────────────────


def _row(sampled, sold, hours, weekday=5, hour=12, store="Costco"):
    return {
        "sampled": sampled,
        "sold": sold,
        "hours": hours,
        "weekday": weekday,
        "hour": hour,
        "store": store,
    }


def test_dayparts():
    assert daypart_for_hour(9) == "Morning"
    assert daypart_for_hour(11) == "Midday"
    assert daypart_for_hour(14) == "Afternoon"
    assert daypart_for_hour(17) == "Evening"


def test_rates_use_only_recaps_with_clocked_hours():
    out = summarize_efficiency(
        [
            _row(400, 40, 4.0),
            # No clock data: counts toward totals, not toward the rate.
            _row(1000, 100, 0.0, store="Kroger"),
        ]
    )
    total = out["total"]
    assert total["sampled"] == 1400 and total["sold"] == 140
    assert total["hours"] == 4.0
    assert total["samples_per_hour"] == 100.0
    assert total["units_per_hour"] == 10.0
    assert out["recaps_without_hours"] == 1


def test_no_hours_means_no_rate_not_zero():
    out = summarize_efficiency([_row(50, 5, 0.0)])
    assert out["total"]["samples_per_hour"] is None
    assert out["total"]["units_per_hour"] is None


def test_groups_by_weekday_daypart_and_store():
    out = summarize_efficiency(
        [
            _row(100, 10, 2.0, weekday=5, hour=10, store="Costco"),
            _row(300, 60, 3.0, weekday=6, hour=15, store="Costco"),
            _row(50, 1, 5.0, weekday=6, hour=18, store="Kroger"),
        ]
    )
    assert [d["label"] for d in out["by_weekday"]] == ["Sat", "Sun"]
    assert [d["label"] for d in out["by_daypart"]] == ["Morning", "Afternoon", "Evening"]
    top = out["top_stores"]
    assert top[0]["label"] == "Costco" and top[0]["sold"] == 70
    assert top[0]["units_per_hour"] == 14.0
    assert top[1]["label"] == "Kroger"


def test_empty_window():
    out = summarize_efficiency([])
    assert out["total"]["recaps"] == 0
    assert out["by_weekday"] == [] and out["top_stores"] == []


def test_cost_per_output_is_none_without_output():
    assert _per(100.0, 0) is None
    assert _per(100.0, 40) == 2.5


# ─── Through the database and the real schema ───────────────────────

GALLERY_Q = """
query G($tid: ID!, $state: String, $cat: String, $paper: Boolean) {
  tenantPhotoGallery(tenantId: $tid, state: $state, category: $cat, includePaperwork: $paper) {
    total
    categories
    states
    photos { url category store state eventDate recapUuid }
  }
}
"""
EFF_Q = """
query E($tid: ID!) {
  tenantFieldEfficiency(tenantId: $tid) {
    total { recaps sampled sold hours samplesPerHour unitsPerHour }
    topStores { label sold }
    recapsWithoutHours
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestInsightsBatchSchema(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Gallery Brand")
        self.other = self.create_tenant(name="Gallery Other")
        self.et = event_models.EventType.objects.create(
            name="Retail Sampling", slug="gallery-retail", tenant=self.tenant,
            created_by=self.sys,
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Gallery Retail", event_type=self.et, tenant=self.tenant,
            created_by=self.sys,
        )
        self.num = recap_models.CustomRecapFieldType.objects.create(
            name="Number", created_by=self.sys
        )
        self.section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.sampled = recap_models.CustomField.objects.create(
            name="Total number of consumers sampled",
            custom_recap_template=self.template, custom_field_type=self.num,
            recap_section=self.section, created_by=self.sys,
        )
        self.sold = recap_models.CustomField.objects.create(
            name="Units sold: Almond Oil",
            custom_recap_template=self.template, custom_field_type=self.num,
            recap_section=self.section, created_by=self.sys,
        )
        self.file_type = recap_models.FileType.objects.create(
            name="image", created_by=self.sys
        )
        self.booth = recap_models.FileRecapCategory.objects.create(
            name="Booth setup", tenant=self.tenant, created_by=self.sys
        )
        self.receipts = recap_models.FileRecapCategory.objects.create(
            name="Expense Receipts", tenant=self.tenant, created_by=self.sys
        )

    def _recap(self, *, day, approved=True, tenant=None, sampled="100", sold="10"):
        tenant = tenant or self.tenant
        when = timezone.make_aware(datetime(2026, 10, day, 18))
        event = event_models.Event.objects.create(
            name=f"Store {day}", tenant=tenant, address="1 Main St", date=when,
            event_type=self.et, created_by=self.sys, updated_by=self.sys,
        )
        recap = recap_models.CustomRecap.objects.create(
            name=f"r{day}", approved=approved, submitted_at=when, event=event,
            tenant=tenant, custom_recap_template=self.template, created_by=self.sys,
        )
        for field, value in ((self.sampled, sampled), (self.sold, sold)):
            recap_models.CustomFieldValue.objects.create(
                custom_recap=recap, custom_field=field, value=value,
                created_by=self.sys,
            )
        return recap

    def _file(self, recap, name, category):
        return recap_models.CustomRecapFile.objects.create(
            name=name, url=f"recap_files/{name}", custom_recap=recap,
            file_type=self.file_type, file_recap_category=category,
            created_by=self.sys,
        )

    def _client_user(self):
        user = self.create_user(
            username="c@gallery.com", email="c@gallery.com",
            role=self.roles["client"], password="password123",
        )
        self.create_tenanted_user(user=user, tenant=self.tenant)
        return user

    @pytest.mark.asyncio
    async def test_gallery_shows_approved_field_photos_and_hides_receipts(
        self, monkeypatch
    ):
        # No bucket in tests: stand in the public-URL builder.
        monkeypatch.setattr(
            "recaps.photo_gallery.public_url", lambda blob: f"https://cdn.test/{blob}"
        )
        monkeypatch.setattr(
            "recaps.photo_gallery.extract_blob_name_from_url", lambda blob: blob
        )
        def _seed():
            live = self._recap(day=5)
            self._file(live, "booth.jpg", self.booth)
            self._file(live, "receipt.jpg", self.receipts)
            self._file(live, "notes.pdf", self.booth)  # not an image
            pending = self._recap(day=6, approved=False)
            self._file(pending, "pending.jpg", self.booth)
            foreign = self._recap(day=7, tenant=self.other)
            self._file(foreign, "foreign.jpg", self.booth)
            return self._client_user()

        me = await sync_to_async(_seed)()
        res = await self._execute_mutation(
            GALLERY_Q, {"tid": str(self.tenant.id)}, self.endpoint_path, user=me
        )
        assert res.errors is None, res.errors
        page = res.data["tenantPhotoGallery"]
        assert page["total"] == 1
        assert [p["url"].rsplit("/", 1)[-1] for p in page["photos"]] == ["booth.jpg"]
        assert page["photos"][0]["category"] == "Booth setup"
        assert page["photos"][0]["eventDate"] == "2026-10-05"
        assert page["categories"] == ["Booth setup"]

        # A client asking for paperwork still does not get receipts.
        sneaky = await self._execute_mutation(
            GALLERY_Q,
            {"tid": str(self.tenant.id), "paper": True},
            self.endpoint_path,
            user=me,
        )
        assert sneaky.data["tenantPhotoGallery"]["total"] == 1

        # …and is pinned to their own brand.
        other = await self._execute_mutation(
            GALLERY_Q, {"tid": str(self.other.id)}, self.endpoint_path, user=me
        )
        urls = [p["url"] for p in other.data["tenantPhotoGallery"]["photos"]]
        assert all("foreign.jpg" not in u for u in urls)

    @pytest.mark.asyncio
    async def test_efficiency_totals_through_the_schema(self):
        def _seed():
            self._recap(day=5, sampled="400", sold="40")
            self._recap(day=6, sampled="100", sold="5")
            self._recap(day=7, approved=False, sampled="999", sold="99")
            return self._client_user()

        me = await sync_to_async(_seed)()
        res = await self._execute_mutation(
            EFF_Q, {"tid": str(self.tenant.id)}, self.endpoint_path, user=me
        )
        assert res.errors is None, res.errors
        eff = res.data["tenantFieldEfficiency"]
        assert eff["total"]["recaps"] == 2
        assert eff["total"]["sampled"] == 500
        assert eff["total"]["sold"] == 45
        # Nobody clocked in, so there is no per-hour rate to show.
        assert eff["total"]["samplesPerHour"] is None
        assert eff["recapsWithoutHours"] == 2
        assert eff["topStores"][0]["sold"] == 40


ISSUE_M = """
mutation I($input: IssueCheckinCodeInput!) {
  issueCheckinCode(input: $input) { success message code url created }
}
"""


@pytest.mark.django_db(transaction=True)
class TestIssueCheckinCode(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Checkin Brand")

    @pytest.mark.asyncio
    async def test_staff_issue_once_and_clients_cannot(self):
        from tenants.models import Tenant

        def _users():
            staff = self.create_user(
                username="s@igniteproductions.co", email="s@igniteproductions.co",
                role=self.roles["spark_admin"], password="password123",
            )
            client = self.create_user(
                username="c@checkin.com", email="c@checkin.com",
                role=self.roles["client"], password="password123",
            )
            self.create_tenanted_user(user=client, tenant=self.tenant)
            return staff, client

        staff, client = await sync_to_async(_users)()
        variables = {"input": {"tenantId": str(self.tenant.id)}}

        denied = await self._execute_mutation(
            ISSUE_M, variables, self.endpoint_path, user=client
        )
        assert denied.errors is None, denied.errors
        assert denied.data["issueCheckinCode"]["success"] is False
        code_now = await sync_to_async(
            lambda: Tenant.objects.get(id=self.tenant.id).checkin_code
        )()
        assert not code_now

        first = await self._execute_mutation(
            ISSUE_M, variables, self.endpoint_path, user=staff
        )
        assert first.errors is None, first.errors
        out = first.data["issueCheckinCode"]
        assert out["success"] is True and out["created"] is True
        assert out["code"] and out["url"].endswith(f"/checkin/{out['code']}")

        again = await self._execute_mutation(
            ISSUE_M, variables, self.endpoint_path, user=staff
        )
        assert again.data["issueCheckinCode"]["created"] is False
        assert again.data["issueCheckinCode"]["code"] == out["code"]
