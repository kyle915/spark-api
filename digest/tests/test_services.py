"""
Tests for the digest aggregator. Focuses on the boundary cases:
empty digest, pending-only, recipient resolution.
"""

import datetime
import pytest
from django.utils import timezone

from digest.services import (
    build_tenant_digest,
    admin_recipients_for_tenant,
)
from events.models import Event, Request
from recaps.models import Recap
from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestDigestServices(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Digest Test Tenant")

    def test_empty_digest_for_empty_tenant(self):
        digest = build_tenant_digest(self.tenant, window_label="Daily")
        assert digest.tenant_id == self.tenant.id
        assert digest.tenant_name == "Digest Test Tenant"
        assert digest.pending_approvals.count == 0
        assert digest.upcoming_shifts.count == 0
        assert digest.unfiled_recaps.count == 0
        assert digest.is_empty is True
        assert digest.total_action_items == 0

    def test_admin_recipients_empty_when_no_admins(self):
        emails = admin_recipients_for_tenant(self.tenant)
        assert emails == []

    def test_admin_recipients_include_admin_users(self):
        admin = self.create_user(
            username="adm-digest",
            email="adm-digest@test.com",
            role=self.roles["spark_admin"],
        )
        self.create_tenanted_user(admin, self.tenant)

        emails = admin_recipients_for_tenant(self.tenant)
        assert emails == ["adm-digest@test.com"]
