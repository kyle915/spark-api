"""Recap-submitted ops mailer CTA must open the filed recap on the client host.

The 9:13 PM KKC mailer's Open recaps button used to mint
``{ADMIN_FRONTEND_URL}/recaps`` — admin, no tenant, not even a real /recaps
route. Clicking it missed the brand recap page. Walk-up filings are
CustomRecap rows; the permalink is ``/recap/view-custom/:uuid`` on
client.igniteproductions.co for every brand, not only KKC.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import timezone

from ambassadors.checkin_web import (
    checkin_recap_open_url,
    notify_checkin_recap_submitted,
)
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps.models import CustomRecap, CustomRecapTemplate, Recap


CLIENT_HOST = "https://client.igniteproductions.co"
RETIRED = (
    "admin.igniteproductions.co",
    "spark.igniteproductions.co",
    "spark-admin.web.app",
)


@pytest.mark.django_db(transaction=True)
class TestCheckinRecapSubmittedMailer(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.actor = self.get_system_user()
        self.tenant = self.create_tenant(name="Krispy Krunchy Chicken")
        self.etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="KKC Sampling",
            event_type=self.etype,
            created_by=self.actor,
        )
        self.event = self.create_event(
            name="8/17/2026 - 4 Jersey Street, Boston, MA 02115",
            tenant=self.tenant,
            address="4 Jersey Street, Boston, MA 02115",
            event_type=self.etype,
            date=timezone.now(),
        )
        ba_user = self.create_user(
            username="francisco@test.com",
            email="francisco@test.com",
            role=self.roles["ambassador"],
            first_name="Francisco",
            last_name="Calva Villalta",
        )
        self.ba = self.create_ambassador(ba_user, phone="8573649770")

    def _custom_recap(self) -> CustomRecap:
        return CustomRecap.objects.create(
            name=self.event.name,
            submitted_at=timezone.now(),
            event=self.event,
            ambassador=self.ba,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.ba.user,
        )

    def test_open_url_is_client_view_custom_permalink(self):
        recap = self._custom_recap()
        url = checkin_recap_open_url(recap)
        assert url == f"{CLIENT_HOST}/recap/view-custom/{recap.uuid}"
        for host in RETIRED:
            assert host not in url
        assert url.endswith(f"/recaps") is False

    def test_open_url_rewrites_spark_and_admin_bases(self, settings):
        recap = self._custom_recap()
        settings.PUBLIC_CHECKIN_BASE_URL = "https://admin.igniteproductions.co"
        settings.ADMIN_FRONTEND_URL = "https://admin.igniteproductions.co"
        settings.CLIENT_FRONTEND_URL = "https://spark.igniteproductions.co"
        url = checkin_recap_open_url(recap)
        assert url.startswith(f"{CLIENT_HOST}/recap/view-custom/")
        for host in RETIRED:
            assert host not in url

    def test_legacy_recap_uses_view_not_view_custom(self):
        recap = Recap(
            uuid="01900000-0000-7000-8000-000000000001",
            name="legacy",
        )
        url = checkin_recap_open_url(recap)
        assert url == (
            f"{CLIENT_HOST}/recap/view/01900000-0000-7000-8000-000000000001"
        )
        assert "/view-custom/" not in url

    def test_missing_uuid_falls_back_to_client_recaps_list(self):
        recap = CustomRecap(
            name="no-uuid",
            event=self.event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.actor,
        )
        recap.uuid = None
        assert checkin_recap_open_url(recap) == f"{CLIENT_HOST}/recaps/list"

    @override_settings(
        CHECKIN_RECAP_NOTIFY_EMAILS=["ops@igniteproductions.co"],
        ADMIN_FRONTEND_URL="https://admin.igniteproductions.co",
        CLIENT_FRONTEND_URL="https://spark.igniteproductions.co",
        PUBLIC_CHECKIN_BASE_URL="https://client.igniteproductions.co",
    )
    def test_mailer_cta_and_body_use_the_permalink(self):
        recap = self._custom_recap()
        captured = {}

        def capture(self):
            captured["envelope"] = self.envelope()

        with patch("utils.mailer.Mailer.send_now", capture):
            notify_checkin_recap_submitted(recap)

        env = captured["envelope"]
        href = f"{CLIENT_HOST}/recap/view-custom/{recap.uuid}"
        assert env.to_emails == ["ops@igniteproductions.co"]
        assert "Recap submitted — Francisco Calva Villalta @" in env.subject
        assert f"href='{href}'" in env.html
        assert "Open recaps" in env.html
        assert f"{CLIENT_HOST}/recaps'" not in env.html
        assert "admin.igniteproductions.co" not in env.html
        assert "spark.igniteproductions.co" not in env.html

    @override_settings(CHECKIN_RECAP_NOTIFY_EMAILS=[])
    def test_no_recipients_skips_send(self):
        recap = self._custom_recap()
        with patch("utils.mailer.Mailer.send_now") as send:
            notify_checkin_recap_submitted(recap)
        send.assert_not_called()
