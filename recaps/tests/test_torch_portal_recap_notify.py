"""Torch portal recap submit notifies requestor + Liberty + events + Nevena."""

from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync

from events import models as em
from events.tests.base import EventsGraphQLTestCase
from recaps import models as recap_models
from recaps.mutation_parts.notify import (
    _collect_recap_approved_recipients,
    _kick_torch_portal_recap_submit_notify,
    is_torch_portal_recap,
)


@pytest.mark.django_db(transaction=True)
class TestTorchPortalRecapNotify(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.torch = self.create_tenant(
            name="Torch THC",
            slug="torch-thc",
            request_url_name="keee-torch-thc",
        )
        self.ld = self.create_tenant(
            name="Liquid Death", slug="liquid-death"
        )
        self.girl = self.create_tenant(name="Girl Beer", slug="girl-beer")
        self.req_approved = self.create_request_status(
            name="Approved", tenant=self.torch, slug="approved", create_event=True
        )
        self.request_type = self.create_request_type(
            name="Retail Sampling", tenant=self.torch
        )
        self.spark_user = self.create_user(
            username="spark-recap@test.com",
            email="spark-recap@test.com",
            role=self.roles["spark_admin"],
        )

    def _torch_request(self, requestor_email="buyer@store.com"):
        return em.Request.objects.create(
            name="Torch portal demo",
            address="123 Main St",
            tenant=self.torch,
            status=self.req_approved,
            request_type=self.request_type,
            requestor_email=requestor_email,
            created_by=None,
        )

    def _make_recap(self, *, tenant, event, name="Recap"):
        return recap_models.Recap.objects.create(
            name=name,
            approved=False,
            event=event,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )

    def test_portal_linked_recap_uses_four_party_list(self):
        req = self._torch_request()
        event = self.create_event(
            name="Torch activation",
            tenant=self.torch,
            address="123 Main St",
            request=req,
        )
        recap = self._make_recap(tenant=self.torch, event=event)
        assert is_torch_portal_recap(recap) is True
        recipients, reply_to = _collect_recap_approved_recipients(recap)
        emails = [e.lower() for e, _ in recipients]
        assert "buyer@store.com" in emails
        assert "liberty@torchdrinks.com" in emails
        assert "events@igniteproductions.co" in emails
        assert "nevena@igniteproductions.co" in emails
        for blast in (
            "kyle@igniteproductions.co",
            "harris@igniteproductions.co",
            "myriant@igniteproductions.co",
            "keis@igniteproductions.co",
        ):
            assert blast not in emails
        assert reply_to == "events@igniteproductions.co"

    def test_standing_torch_recap_without_request_is_not_portal(self):
        event = self.create_event(
            name="TH-2HRV3D standing",
            tenant=self.torch,
            address="Warehouse",
        )
        recap = self._make_recap(tenant=self.torch, event=event, name="Standing")
        assert is_torch_portal_recap(recap) is False

    def test_liquid_death_recap_is_not_torch_portal(self):
        event = self.create_event(
            name="LD demo", tenant=self.ld, address="NY"
        )
        recap = self._make_recap(tenant=self.ld, event=event, name="LD")
        assert is_torch_portal_recap(recap) is False

    def test_girl_beer_recap_is_not_torch_portal(self):
        event = self.create_event(
            name="GB demo", tenant=self.girl, address="Austin"
        )
        recap = self._make_recap(tenant=self.girl, event=event, name="GB")
        assert is_torch_portal_recap(recap) is False

    def test_submit_kick_sends_four_party_and_stamps(self):
        req = self._torch_request()
        event = self.create_event(
            name="Torch activation",
            tenant=self.torch,
            address="123 Main St",
            request=req,
        )
        recap = self._make_recap(tenant=self.torch, event=event)
        sent_to: list[str] = []

        def _record_send(self, *args, **kwargs):
            sent_to.extend(self.to_emails)

        with (
            patch(
                "recaps.mutation_parts.notify.enqueue",
                return_value=False,
            ),
            patch(
                "recaps.mutation_parts.notify._ensure_recap_pdf_for_notify",
                new_callable=AsyncMock,
            ) as ensure_pdf,
            patch(
                "recaps.mutation_parts.notify.RecapApprovedNotificationMailer.send",
                new=_record_send,
            ),
        ):
            async_to_sync(_kick_torch_portal_recap_submit_notify)(recap, "legacy")
        ensure_pdf.assert_not_called()
        lowered = {e.lower() for e in sent_to}
        assert lowered == {
            "buyer@store.com",
            "liberty@torchdrinks.com",
            "events@igniteproductions.co",
            "nevena@igniteproductions.co",
        }
        recap.refresh_from_db()
        assert recap.client_notified_at is not None

    def test_submit_kick_skips_non_portal_and_girl_beer(self):
        ld_event = self.create_event(name="LD", tenant=self.ld, address="NY")
        ld_recap = self._make_recap(tenant=self.ld, event=ld_event, name="LD")
        gb_event = self.create_event(name="GB", tenant=self.girl, address="TX")
        gb_recap = self._make_recap(tenant=self.girl, event=gb_event, name="GB")
        with (
            patch(
                "recaps.mutation_parts.notify.enqueue",
                return_value=False,
            ),
            patch(
                "recaps.mutation_parts.notify.RecapApprovedNotificationMailer.send"
            ) as mock_send,
        ):
            async_to_sync(_kick_torch_portal_recap_submit_notify)(ld_recap, "legacy")
            async_to_sync(_kick_torch_portal_recap_submit_notify)(gb_recap, "legacy")
        mock_send.assert_not_called()
        ld_recap.refresh_from_db()
        gb_recap.refresh_from_db()
        assert ld_recap.client_notified_at is None
        assert gb_recap.client_notified_at is None


@pytest.mark.django_db(transaction=True)
class TestTorchPortalRecapLink(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.torch = self.create_tenant(
            name="Torch THC",
            slug="torch-thc",
            request_url_name="keee-torch-thc",
        )
        self.spark_user = self.create_user(
            username="spark-link@test.com",
            email="spark-link@test.com",
            role=self.roles["spark_admin"],
        )

    def test_recap_mailer_link_is_client_host(self):
        from recaps.envelopes import RecapApprovedNotificationMailer

        event = self.create_event(
            name="Torch activation", tenant=self.torch, address="Austin"
        )
        recap = recap_models.Recap.objects.create(
            name="Portal recap",
            event=event,
            created_by=self.spark_user,
        )
        env = RecapApprovedNotificationMailer(
            recap=recap,
            to_emails=["buyer@store.com"],
        ).envelope()
        assert env.subject == "Your activation recap is ready"
        assert env.context["recap_link"].startswith(
            "https://client.igniteproductions.co/r/"
        )
        assert "admin.igniteproductions.co" not in env.context["recap_link"]
