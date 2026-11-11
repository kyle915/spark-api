"""Per-person email preferences: opt-out, caller-only, and only for the
optional categories. Account and shift mail must never be filtered."""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

from config.schema_client import schema_clients
from tenants.models import NotificationPreference
from tenants.tests.base import BaseGraphQLTestCase
from utils.mailer import Envelope
from utils.notification_prefs import (
    CATEGORIES,
    category_for_template,
    drop_muted_recipients,
)

SET_M = """
mutation Set($input: SetMyNotificationPreferenceInput!) {
  setMyNotificationPreference(input: $input) {
    success message category emailEnabled
  }
}
"""
GET_Q = """
query Mine { myNotificationPreferences { category label emailEnabled } }
"""
MENTION = "events.templates.emails.note_mention"


@pytest.mark.django_db(transaction=True)
class TestNotificationPreferences(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.tenant = self.create_tenant(name="Prefs Brand")

    def _user(self, email):
        user = self.create_user(
            username=email, email=email, role=self.roles["client"],
            password="password123",
        )
        self.create_tenanted_user(user=user, tenant=self.tenant)
        return user

    def test_templates_map_to_one_category_each(self):
        seen = [t for c in CATEGORIES for t in c.templates]
        assert len(seen) == len(set(seen))
        assert category_for_template(MENTION) == "mentions"

    def test_account_and_shift_mail_is_never_a_category(self):
        for template in (
            "tenants.templates.emails.email_verification",
            "tenants.templates.emails.password_reset",
            "tenants.templates.emails.magic_link",
            "tenants.templates.emails.client_invite",
            "events.templates.emails.event_confirmation",
            "jobs.templates.emails.ambassador_assigned_to_job_default",
            "",
            None,
        ):
            assert category_for_template(template) is None

    def test_no_row_means_the_email_still_goes(self):
        self._user("a@brand.com")
        env = Envelope(template=MENTION, to_emails=["a@brand.com"])
        drop_muted_recipients(env)
        assert env.to_emails == ["a@brand.com"]

    def test_muting_drops_only_that_person_and_category(self):
        a = self._user("a@brand.com")
        self._user("b@brand.com")
        NotificationPreference.objects.create(
            user=a, category="mentions", email_enabled=False
        )
        env = Envelope(
            template=MENTION,
            to_emails=["A@Brand.com", "b@brand.com"],
            cc_emails=["outside@other.com"],
        )
        drop_muted_recipients(env)
        assert env.to_emails == ["b@brand.com"]
        assert env.cc_emails == ["outside@other.com"]

        other = Envelope(
            template="events.templates.emails.request_approved_notification",
            to_emails=["a@brand.com"],
        )
        drop_muted_recipients(other)
        assert other.to_emails == ["a@brand.com"]

        account = Envelope(
            template="tenants.templates.emails.password_reset",
            to_emails=["a@brand.com"],
        )
        drop_muted_recipients(account)
        assert account.to_emails == ["a@brand.com"]

    def test_muted_to_promotes_remaining_cc(self):
        a = self._user("a@brand.com")
        NotificationPreference.objects.create(
            user=a, category="mentions", email_enabled=False
        )
        env = Envelope(
            template=MENTION, to_emails=["a@brand.com"], cc_emails=["b@brand.com"]
        )
        drop_muted_recipients(env)
        assert env.to_emails == ["b@brand.com"]
        assert env.cc_emails == []

    def test_re_enabling_restores_delivery(self):
        a = self._user("a@brand.com")
        NotificationPreference.objects.create(
            user=a, category="mentions", email_enabled=True
        )
        env = Envelope(template=MENTION, to_emails=["a@brand.com"])
        drop_muted_recipients(env)
        assert env.to_emails == ["a@brand.com"]

    @pytest.mark.asyncio
    async def test_lookup_works_inside_an_event_loop(self):
        a = await sync_to_async(self._user)("a@brand.com")
        await sync_to_async(NotificationPreference.objects.create)(
            user=a, category="mentions", email_enabled=False
        )
        env = Envelope(template=MENTION, to_emails=["a@brand.com", "x@y.com"])
        drop_muted_recipients(env)
        assert env.to_emails == ["x@y.com"]

    @pytest.mark.asyncio
    async def test_graphql_defaults_on_then_mutes_caller_only(self):
        me = await sync_to_async(self._user)("me@brand.com")
        other = await sync_to_async(self._user)("other@brand.com")

        got = await self._execute_mutation(GET_Q, {}, self.endpoint_path, user=me)
        assert got.errors is None, got.errors
        items = got.data["myNotificationPreferences"]
        assert [i["category"] for i in items] == [c.key for c in CATEGORIES]
        assert all(i["emailEnabled"] for i in items)

        res = await self._execute_mutation(
            SET_M,
            {"input": {"category": "digests", "emailEnabled": False}},
            self.endpoint_path,
            user=me,
        )
        assert res.errors is None, res.errors
        assert res.data["setMyNotificationPreference"]["success"] is True
        assert res.data["setMyNotificationPreference"]["emailEnabled"] is False

        rows = await sync_to_async(
            lambda: list(
                NotificationPreference.objects.values_list(
                    "user__email", "category", "email_enabled"
                )
            )
        )()
        assert rows == [("me@brand.com", "digests", False)]

        mine = await self._execute_mutation(GET_Q, {}, self.endpoint_path, user=me)
        state = {
            i["category"]: i["emailEnabled"]
            for i in mine.data["myNotificationPreferences"]
        }
        assert state["digests"] is False and state["mentions"] is True

        theirs = await self._execute_mutation(
            GET_Q, {}, self.endpoint_path, user=other
        )
        assert all(
            i["emailEnabled"] for i in theirs.data["myNotificationPreferences"]
        )

    @pytest.mark.asyncio
    async def test_unknown_category_is_rejected_without_a_row(self):
        me = await sync_to_async(self._user)("me@brand.com")
        res = await self._execute_mutation(
            SET_M,
            {"input": {"category": "password_reset", "emailEnabled": False}},
            self.endpoint_path,
            user=me,
        )
        assert res.errors is None, res.errors
        assert res.data["setMyNotificationPreference"]["success"] is False
        count = await sync_to_async(NotificationPreference.objects.count)()
        assert count == 0
