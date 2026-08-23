from unittest.mock import patch

import pytest
from django.utils import timezone

from events import models as em
from events.tests.base import EventsGraphQLTestCase
from tenants.models import Tenant
from utils.sheets_mirror import _record_sheet_sync, upsert_request_row


@pytest.mark.django_db(transaction=True)
class TestSheetsMirrorSyncStatus(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Sheet Sync Tenant")
        self.sys = self.get_system_user()
        self.tenant.linked_sheet_url = (
            "https://docs.google.com/spreadsheets/d/abc123def456/edit"
        )
        self.tenant.save(update_fields=["linked_sheet_url"])
        rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.request = em.Request.objects.create(
            name="Mirror me",
            address="1 Test St",
            request_type=rt,
            tenant=self.tenant,
            created_by=self.sys,
        )

    def test_record_sheet_sync_success(self):
        _record_sheet_sync(self.tenant, self.request, ok=True)
        refreshed = Tenant.objects.get(pk=self.tenant.pk)
        assert refreshed.linked_sheet_last_sync_at is not None
        assert refreshed.linked_sheet_last_sync_error is None
        assert refreshed.linked_sheet_last_request_id == self.request.id

    def test_record_sheet_sync_failure(self):
        _record_sheet_sync(
            self.tenant,
            self.request,
            ok=False,
            error="permission denied",
        )
        refreshed = Tenant.objects.get(pk=self.tenant.pk)
        assert refreshed.linked_sheet_last_sync_error == "permission denied"
        assert refreshed.linked_sheet_last_request_id == self.request.id

    @patch("utils.sheets_mirror._service", return_value=None)
    def test_upsert_records_failure_when_service_missing(self, _svc):
        assert upsert_request_row(self.request) is False
        refreshed = Tenant.objects.get(pk=self.tenant.pk)
        assert "unavailable" in (refreshed.linked_sheet_last_sync_error or "").lower()
