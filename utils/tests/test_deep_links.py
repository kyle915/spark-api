"""Emailed links must open the client the email is about.

The web app keeps one selected client per browser. A staff member who last
worked in Liquid Death and clicked a Breakaway email landed on Breakaway
content inside Liquid Death's switcher, public link and sidebar counts.

Two halves, both pinned here:

* LIST links (``/walkups``, ``/recaps/list``, digest lists) carry
  ``?t=<tenant uuid>`` — ``utils.deep_links.with_tenant``.
* RECORD links (``/recap/view-custom/:uuid`` …) also stamp ``?t=`` as a
  first-paint hint; the page still reads the record's own tenant and
  switches to it. That only works if the real clients schema hands back
  the tenant on each record, with the SAME id/uuid the switcher's
  ``tenants`` list uses — the schema test below.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as _tz
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings

from ambassadors.checkin_web import (
    _email_admins_checkin_landed,
    _email_admins_shift_moved_brand,
    checkin_recap_open_url,
)
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as recap_models
from utils.deep_links import with_tenant

UUID = "01900000-0000-7000-8000-0000000000aa"


class TestWithTenant:
    def test_appends_the_tenant_uuid(self):
        tenant = SimpleNamespace(uuid=UUID)
        assert (
            with_tenant("https://admin.x/walkups", tenant)
            == f"https://admin.x/walkups?t={UUID}"
        )

    def test_keeps_existing_query_and_fragment(self):
        tenant = SimpleNamespace(uuid=UUID)
        assert (
            with_tenant("https://c.x/recaps/list?activation=retail#top", tenant)
            == f"https://c.x/recaps/list?activation=retail&t={UUID}#top"
        )
        assert (
            with_tenant("https://a.x/approvals?request=12", tenant)
            == f"https://a.x/approvals?request=12&t={UUID}"
        )

    def test_unchanged_without_a_tenant_or_a_url(self):
        assert with_tenant("https://a.x/walkups", None) == "https://a.x/walkups"
        assert (
            with_tenant("https://a.x/walkups", SimpleNamespace(uuid=None))
            == "https://a.x/walkups"
        )
        assert with_tenant("", SimpleNamespace(uuid=UUID)) == ""
        assert with_tenant(None, SimpleNamespace(uuid=UUID)) == ""

    def test_never_doubles_the_param(self):
        assert (
            with_tenant("https://a.x/walkups?t=already", SimpleNamespace(uuid=UUID))
            == "https://a.x/walkups?t=already"
        )
        # Legacy ``?tenant=`` emails must not also get ``?t=``.
        assert (
            with_tenant(
                "https://a.x/walkups?tenant=already", SimpleNamespace(uuid=UUID)
            )
            == "https://a.x/walkups?tenant=already"
        )


RECORD_TENANT_QUERY = """
query RecordTenants($customUuid: ID!, $recapUuid: ID!, $eventUuid: ID!) {
  customRecap(uuid: $customUuid) {
    event { tenant { id uuid } }
    customRecapTemplate { tenant { id uuid } }
  }
  recap(uuid: $recapUuid) {
    event { tenant { id uuid name slug } }
  }
  event(uuid: $eventUuid) {
    tenantId
    tenant { id uuid }
  }
  tenants(first: 500) {
    edges { node { id uuid name } }
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestEmailLinksNameTheirTenant(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()
        self.selected = self.create_tenant(name="Liquid Death")
        self.tenant = self.create_tenant(name="Breakaway")
        self.spark_admin = self.create_user(
            username="admin-deep-links",
            email="admin-deep-links@test.com",
            role=self.roles["spark_admin"],
        )
        now = datetime.now(_tz.utc)
        self.event_type = self.create_event_type("Event Activation", self.tenant)
        self.event = self.create_event(
            name="11/14/2026 - 801 Saint Emanuel Street, Houston, TX 77003",
            tenant=self.tenant,
            address="801 Saint Emanuel Street, Houston, TX 77003",
            event_type=self.event_type,
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Breakaway · Jimmy Johns Recap",
            event_type=self.event_type,
            created_by=self.system_user,
        )
        ba_user = self.create_user(
            username="ba-deep-links@test.com",
            email="ba-deep-links@test.com",
            role=self.roles["ambassador"],
            first_name="Dana",
            last_name="Field",
        )
        self.ba = self.create_ambassador(ba_user, phone="5555550100")

    # ── list links ────────────────────────────────────────────────────

    def test_tenant_id_resolves_to_the_uuid(self):
        assert (
            with_tenant("https://a.x/recaps/list", tenant_id=self.tenant.id)
            == f"https://a.x/recaps/list?t={self.tenant.uuid}"
        )
        assert (
            with_tenant("https://a.x/recaps/list", tenant_id=10**9)
            == "https://a.x/recaps/list"
        )

    @override_settings(
        PUBLIC_CHECKIN_BASE_URL="https://client.igniteproductions.co",
    )
    def test_recap_list_fallback_names_the_tenant(self):
        recap = recap_models.CustomRecap(
            name="no-uuid",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.system_user,
        )
        recap.uuid = None
        assert checkin_recap_open_url(recap) == (
            "https://client.igniteproductions.co/recaps/list"
            f"?t={self.tenant.uuid}"
        )

    @override_settings(
        CHECKIN_HOURS_NOTIFY_EMAILS=["events@igniteproductions.co"],
        ADMIN_FRONTEND_URL="https://admin.igniteproductions.co",
    )
    def test_walkup_alerts_open_the_events_tenant_queue(self):
        captured = []

        def capture(mailer):
            captured.append(mailer.envelope())

        with patch("utils.mailer.Mailer.send_now", capture):
            _email_admins_checkin_landed(self.event, self.ba)
            _email_admins_shift_moved_brand(self.event, self.ba, self.selected)

        assert len(captured) == 2
        href = (
            "href='https://admin.igniteproductions.co/walkups"
            f"?t={self.tenant.uuid}'"
        )
        for env in captured:
            assert href in env.html
            # never the brand the BA's link belonged to / the reader's last one
            assert str(self.selected.uuid) not in env.html

    def test_admin_digest_lists_name_the_tenant(self):
        from digest.envelopes import AdminDigestMailer, ExecutiveSummaryMailer

        mailer = AdminDigestMailer.__new__(AdminDigestMailer)
        mailer.digest = SimpleNamespace(tenant_id=self.tenant.id)
        mailer.web_app_base_url = "https://admin.igniteproductions.co"
        suffix = f"?t={self.tenant.uuid}"
        assert mailer._link("/recaps/list") == (
            f"https://admin.igniteproductions.co/recaps/list{suffix}"
        )
        summary = ExecutiveSummaryMailer.__new__(ExecutiveSummaryMailer)
        summary.summary = SimpleNamespace(tenant_id=self.tenant.id)
        summary.web_app_base_url = "https://admin.igniteproductions.co"
        assert summary._link("/reports") == (
            f"https://admin.igniteproductions.co/reports{suffix}"
        )

    def test_client_digest_lists_name_the_tenant(self):
        from recaps.envelopes import _tenant_link

        payload = SimpleNamespace(tenant_id=self.tenant.id)
        assert _tenant_link(
            "https://client.igniteproductions.co/recaps/list?activation=retail",
            payload,
        ) == (
            "https://client.igniteproductions.co/recaps/list"
            f"?activation=retail&t={self.tenant.uuid}"
        )

    # ── record links: the page switches to the record's own tenant ────

    @pytest.mark.asyncio
    async def test_records_expose_their_tenant_through_the_real_schema(self):
        """Exactly the tenant selections the web detail pages make.

        An admin "switched to" Liquid Death opens Breakaway records; each
        must report Breakaway, with the id/uuid the switcher list has for
        it, so the page can find it in that list and switch.
        """

        def make():
            custom = recap_models.CustomRecap.objects.create(
                name=self.event.name,
                approved=True,
                event=self.event,
                ambassador=self.ba,
                tenant=self.tenant,
                custom_recap_template=self.template,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            legacy = recap_models.Recap.objects.create(
                name="Legacy recap",
                approved=True,
                event=self.event,
                created_by=self.system_user,
                updated_by=self.system_user,
            )
            return custom, legacy

        custom, legacy = await sync_to_async(make)()
        result = await self._execute_mutation_authenticated(
            RECORD_TENANT_QUERY,
            {
                "customUuid": str(custom.uuid),
                "recapUuid": str(legacy.uuid),
                "eventUuid": str(self.event.uuid),
            },
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        data = result.data

        switcher = {
            edge["node"]["name"]: edge["node"]
            for edge in data["tenants"]["edges"]
        }
        want = switcher["Breakaway"]
        assert want["uuid"] == str(self.tenant.uuid)
        assert want["id"] != switcher["Liquid Death"]["id"]

        def same(node):
            return node["id"] == want["id"] and node["uuid"] == want["uuid"]

        assert same(data["customRecap"]["event"]["tenant"])
        assert same(data["customRecap"]["customRecapTemplate"]["tenant"])
        assert same(data["recap"]["event"]["tenant"])
        assert same(data["event"]["tenant"])
