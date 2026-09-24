"""Torch retail recap recipients follow the by-state sales org."""

from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync

from events import models as em
from events.tests.base import EventsGraphQLTestCase
from events.torch_retail_routing import (
    torch_retail_recap_emails,
    torch_weekly_digest_emails,
)
from recaps import models as recap_models
from recaps.mutation_parts.notify import (
    _collect_recap_approved_recipients,
    _kick_torch_portal_recap_submit_notify,
    is_torch_portal_recap,
)


@pytest.mark.django_db(transaction=True)
class TestTorchRetailStateRouting(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self):
        self.roles = self.setup_default_roles()
        self.system_user = self.get_system_user()
        self.torch = self.create_tenant(
            name="Torch THC",
            slug="torch-thc",
            request_url_name="keee-torch-thc",
            # Client-role user must NOT land on per-recap if not on the map.
            recap_recipient_emails="ryanheuser@torchdrinks.com, stray@torchdrinks.com",
        )
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
        self.ryan = self.create_user(
            username="ryan",
            email="ryanheuser@torchdrinks.com",
            role=self.roles["client"],
            first_name="Ryan",
        )
        self.create_tenanted_user(self.ryan, self.torch)

    def _recap(self, *, address: str, request=None, name="Recap"):
        event = self.create_event(
            name=name,
            tenant=self.torch,
            address=address,
            request=request,
        )
        return recap_models.Recap.objects.create(
            name=name,
            approved=False,
            event=event,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )

    def test_florida_standing_includes_fl_team_and_excludes_ryan(self):
        recap = self._recap(
            address="100 Ocean Dr, Miami Beach, FL 33139",
            name="Standing FL",
        )
        assert is_torch_portal_recap(recap) is False
        recipients, reply_to = _collect_recap_approved_recipients(recap)
        emails = {e.lower() for e, _ in recipients}
        assert emails == {
            "john@torchdrinks.com",
            "doug@torchdrinks.com",
            "liberty@torchdrinks.com",
            "james@torchdrinks.com",
            "leslyann@torchdrinks.com",
            "emily@torchdrinks.com",
        }
        assert "ryanheuser@torchdrinks.com" not in emails
        assert "cesar@torchdrinks.com" not in emails
        assert "stray@torchdrinks.com" not in emails
        assert reply_to == "events@igniteproductions.co"

    def test_north_carolina_includes_cesar_and_lucas(self):
        recap = self._recap(address="200 Main St, Raleigh, NC 27601")
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert "cesar@torchdrinks.com" in emails
        assert "lucas@torchdrinks.com" in emails
        assert "skylar@torchdrinks.com" not in emails
        assert "bobby@torchdrinks.com" not in emails

    def test_unknown_state_gets_all_states_only(self):
        recap = self._recap(address="Warehouse bay 3")
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert emails == {
            "john@torchdrinks.com",
            "doug@torchdrinks.com",
            "liberty@torchdrinks.com",
        }

    def test_portal_tx_adds_requestor_and_ignite_ops(self):
        req = em.Request.objects.create(
            name="Torch portal demo",
            address="500 Congress Ave, Austin, TX 78701",
            tenant=self.torch,
            status=self.req_approved,
            request_type=self.request_type,
            requestor_email="buyer@store.com",
            created_by=None,
        )
        recap = self._recap(
            address="500 Congress Ave, Austin, TX 78701",
            request=req,
            name="Portal TX",
        )
        assert is_torch_portal_recap(recap) is True
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert "buyer@store.com" in emails
        assert "brad@torchdrinks.com" in emails
        assert "morgan@torchdrinks.com" in emails
        assert "events@igniteproductions.co" in emails
        assert "nevena@igniteproductions.co" in emails
        assert "ryanheuser@torchdrinks.com" not in emails
        for blast in (
            "kyle@igniteproductions.co",
            "harris@igniteproductions.co",
            "myriant@igniteproductions.co",
            "keis@igniteproductions.co",
        ):
            assert blast not in emails

    def test_portal_submit_kick_uses_state_list(self):
        req = em.Request.objects.create(
            name="Torch portal demo",
            address="500 Congress Ave, Austin, TX 78701",
            tenant=self.torch,
            status=self.req_approved,
            request_type=self.request_type,
            requestor_email="buyer@store.com",
            created_by=None,
        )
        recap = self._recap(
            address="500 Congress Ave, Austin, TX 78701",
            request=req,
        )
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
            ),
            patch(
                "recaps.mutation_parts.notify.RecapApprovedNotificationMailer.send",
                new=_record_send,
            ),
        ):
            async_to_sync(_kick_torch_portal_recap_submit_notify)(recap, "legacy")
        lowered = {e.lower() for e in sent_to}
        assert "buyer@store.com" in lowered
        assert "brad@torchdrinks.com" in lowered
        assert "liberty@torchdrinks.com" in lowered
        assert "ryanheuser@torchdrinks.com" not in lowered
        recap.refresh_from_db()
        assert recap.client_notified_at is not None

    def test_weekly_command_uses_torch_coded_list(self):
        from django.core.management import call_command
        from unittest import mock
        from tenants.management.commands import send_client_weekly_digest as cmd_mod

        type(self.torch).objects.filter(id=self.torch.id).update(
            client_weekly_digest_enabled=True
        )
        captured = []

        class _FakeMailer:
            def __init__(self, **kwargs):
                captured.append(kwargs)

            def send(self):
                return None

        with (
            mock.patch.object(cmd_mod, "ClientWeeklyDigestMailer", _FakeMailer),
            mock.patch.object(cmd_mod, "build_weekly_digest") as build,
        ):
            build.return_value = mock.Mock(
                has_content=True,
                completed_activations=1,
                upcoming_total=0,
            )
            call_command("send_client_weekly_digest", f"--tenant={self.torch.id}", "--force")
        assert len(captured) == 1
        recipients = {e.lower() for e in captured[0]["recipients"]}
        assert "ryanheuser@torchdrinks.com" in recipients
        assert "john@torchdrinks.com" in recipients
        assert "james@torchdrinks.com" in recipients
