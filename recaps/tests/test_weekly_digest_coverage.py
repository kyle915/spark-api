"""Weekly digest extensions: shared /r/ links + store coverage + recipients."""

import datetime
from unittest import mock

import pytest
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as em
from recaps import models as rm
from recaps.weekly_digest import build_weekly_digest, client_digest_recipients

NOW = timezone.make_aware(datetime.datetime(2026, 6, 15, 10, 30, 0))


@pytest.mark.django_db(transaction=True)
class TestWeeklyDigestExtensions(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Digest Coverage",
            client_weekly_digest_enabled=True,
            recap_recipient_emails="brand@x.com",
        )
        self.state = em.State.objects.create(name="Texas", code="TX", created_by=self.sys)
        self.loc = em.Location.objects.create(name="Austin", code="AUS", zip="78701", state=self.state, created_by=self.sys)
        self.visited = em.Retailer.objects.create(name="Visited", address="1 St", location=self.loc, tenant=self.tenant, created_by=self.sys)
        self.never = em.Retailer.objects.create(name="Neverland", address="2 St", location=self.loc, tenant=self.tenant, created_by=self.sys)
        ev = self.create_event(name="shift", tenant=self.tenant)
        em.Event.objects.filter(pk=ev.pk).update(retailer=self.visited, start_time=NOW - datetime.timedelta(days=2))
        self.shared = rm.Recap.objects.create(
            name="Shared recap", event_id=ev.pk, created_by=self.sys, updated_by=self.sys,
            approved=True, total_engagements=9,
            shared_at=NOW - datetime.timedelta(days=1),
        )
        self.unapproved_shared = rm.Recap.objects.create(
            name="Secret draft", event_id=ev.pk, created_by=self.sys, updated_by=self.sys,
            approved=False, total_engagements=9,
            shared_at=NOW - datetime.timedelta(days=1),
        )

    def test_shared_links_only_approved_with_client_host_tokens(self):
        d = build_weekly_digest(self.tenant.id, NOW)
        names = [s.name for s in d.shared_recaps]
        assert names == ["Shared recap"]
        assert "/r/" in d.shared_recaps[0].url
        assert "spark.igniteproductions.co" not in d.shared_recaps[0].url
        assert "admin.igniteproductions.co" not in d.shared_recaps[0].url
        from recaps.recap_tokens import verify_recap_token

        assert verify_recap_token(d.shared_recaps[0].url.rsplit("/r/", 1)[1]) == (
            "legacy",
            self.shared.pk,
        )
        assert d.has_content is True

    def test_coverage_counts_roster(self):
        d = build_weekly_digest(self.tenant.id, NOW)
        # Filed? shared recap has total_engagements -> filed, so Visited covered.
        assert d.coverage.roster_total == 2
        assert d.coverage.covered == 1
        assert d.coverage.never_visited == 1
        assert d.coverage.never_names == ["Neverland"]

    def test_recipients_include_client_role_users(self):
        client = self.create_user(username="cli-d", email="person@brand.com", role=self.roles["client"])
        self.create_tenanted_user(client, self.tenant)
        emails = client_digest_recipients(self.tenant)
        assert "brand@x.com" in emails
        assert "person@brand.com" in emails
