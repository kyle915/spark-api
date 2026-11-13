"""A brand contact who keeps their login but is left off the brand's
approved-recap email (Wook Water, Nov 2026: one person sits on two sister
brands and should only get the other brand's recaps)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from django.utils import timezone as dj_tz

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event
from recaps.mutation_parts.notify import _collect_recap_approved_recipients


@pytest.mark.django_db(transaction=True)
class TestRecapSkipEmails(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.sys = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Wook {uid}")
        self.sister = self.create_tenant(name=f"Famus {uid}")
        self.keep = self.create_user(
            username=f"keep-{uid}@brand.com", email=f"keep-{uid}@brand.com",
            role=self.roles["client"], first_name="Brit", last_name="C",
        )
        self.both = self.create_user(
            username=f"both-{uid}@brand.com", email=f"both-{uid}@brand.com",
            role=self.roles["client"], first_name="Lee", last_name="K",
        )
        self.create_tenanted_user(self.keep, self.tenant)
        self.create_tenanted_user(self.both, self.tenant)
        self.create_tenanted_user(self.both, self.sister)

    def _emails(self, tenant):
        event = Event.objects.create(
            tenant=tenant, name="Store", address="1 Main St",
            date=dj_tz.now(), created_by=self.sys,
        )
        recipients, _reply = _collect_recap_approved_recipients(
            SimpleNamespace(event=event)
        )
        return {e.lower() for e, _first in recipients}

    def test_skipped_person_is_left_off_this_brand_only(self):
        assert self._emails(self.tenant) == {self.keep.email, self.both.email}

        # Skipped however the address is written, and even when it is also
        # on the brand's extra-recipient list.
        self.tenant.recap_skip_emails = f" {self.both.email.upper()} ;"
        self.tenant.recap_recipient_emails = f"{self.both.email}, extra@brand.com"
        self.tenant.save()

        assert self._emails(self.tenant) == {self.keep.email, "extra@brand.com"}
        # The sister brand still mails them.
        assert self._emails(self.sister) == {self.both.email}
