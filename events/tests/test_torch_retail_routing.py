"""Torch retail recap recipients follow the by-state sales org."""

from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync

from events import models as em
from events.tests.base import EventsGraphQLTestCase
from events.torch_retail_routing import (
    torch_field_marketing_recap_emails,
    torch_recap_execution_type,
    torch_retail_recap_emails,
    torch_weekly_digest_emails,
)
from recaps import models as recap_models
from recaps.mutation_parts.notify import (
    _collect_recap_approved_recipients,
    _kick_torch_portal_recap_submit_notify,
    is_torch_portal_recap,
)

FIELD_MARKETING = {
    "ryanheuser@torchdrinks.com",
    "alec@torchdrinks.com",
    "brittany@torchdrinks.com",
    "victoria@torchdrinks.com",
    "octavius@torchdrinks.com",
}


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

    def test_ohio_includes_jason_plus_always_on(self):
        for address in (
            "1 Easton Way, Columbus, OH 43219",
            "1 Easton Way, Columbus, Ohio, United States",
        ):
            recap = self._recap(address=address, name=f"OH {address}")
            emails = {
                e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]
            }
            assert emails == {
                "john@torchdrinks.com",
                "doug@torchdrinks.com",
                "liberty@torchdrinks.com",
                "jason@torchdrinks.com",
            }

    def test_street_named_after_a_state_routes_by_the_real_state(self):
        always = {
            "john@torchdrinks.com",
            "doug@torchdrinks.com",
            "liberty@torchdrinks.com",
        }
        # Old parser read "Georgia Ave" as GA (Cesar) on an Ohio store.
        recap = self._recap(address="100 Georgia Ave, Columbus, OH 43215", name="OH Georgia Ave")
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert emails == always | {"jason@torchdrinks.com"}

        # No state in the address: the event's stored state wins, not the street.
        ohio = em.State.objects.create(name="Ohio", code="OH", created_by=self.system_user)
        recap = self._recap(address="13657 Texas Ave", name="OH Texas Ave")
        recap.event.state = ohio
        recap.event.save(update_fields=["state"])
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert emails == always | {"jason@torchdrinks.com"}

        # "Peachtree Rd NE" is a street direction; the glued zip carries GA.
        recap = self._recap(
            address="3954A PEACHTREE ROAD NE, , BROOKHAVEN, GA30319", name="GA Peachtree"
        )
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert emails == always | {"cesar@torchdrinks.com"}

    def test_non_ohio_excludes_jason(self):
        recap = self._recap(address="100 Ocean Dr, Miami Beach, FL 33139")
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert "jason@torchdrinks.com" not in emails
        assert "jason@torchdrinks.com" not in torch_retail_recap_emails("MO")
        assert "jason@torchdrinks.com" in torch_retail_recap_emails("oh")
        assert "jason@torchdrinks.com" in torch_weekly_digest_emails()

    def _custom_recap(
        self, *, template_name: str, event_type_name: str, address: str, request=None
    ):
        event = self.create_event(
            name=template_name,
            tenant=self.torch,
            address=address,
            request=request,
        )
        template = recap_models.CustomRecapTemplate.objects.create(
            name=template_name,
            event_type=self.create_event_type(name=event_type_name, tenant=self.torch),
            tenant=self.torch,
            created_by=self.system_user,
        )
        return recap_models.CustomRecap.objects.create(
            name=template_name,
            event=event,
            tenant=self.torch,
            custom_recap_template=template,
            created_by=self.spark_user,
            updated_by=self.spark_user,
        )

    NON_RETAIL = (
        ("Torch THC-Event Activation", "Event Activation", "event"),
        ("Torch THC · Guerilla Recap", "Guerilla Activation", "guerilla"),
        ("Torch THC · Product Seeding Recap", "Product Seeding", "seeding"),
    )

    def test_event_guerilla_seeding_go_only_to_field_marketing_list(self):
        for template_name, type_name, kind in self.NON_RETAIL:
            for address in (
                "500 Vine St, Cincinnati, OH 45202",
                "100 Ocean Dr, Miami Beach, FL 33139",
                "Warehouse bay 3",
            ):
                recap = self._custom_recap(
                    template_name=template_name,
                    event_type_name=type_name,
                    address=address,
                )
                assert torch_recap_execution_type(recap) == kind
                recipients, reply_to = _collect_recap_approved_recipients(recap)
                assert {e.lower() for e, _ in recipients} == FIELD_MARKETING, (
                    template_name,
                    address,
                )
                assert reply_to == "events@igniteproductions.co"

    def test_non_retail_portal_adds_ignite_ops_not_requestor(self):
        event_activation = self.create_request_type(
            name="Event Activation", tenant=self.torch
        )
        req = em.Request.objects.create(
            name="Torch plan activation",
            address="500 Congress Ave, Austin, TX 78701",
            tenant=self.torch,
            status=self.req_approved,
            request_type=event_activation,
            requestor_email="planner@torchdrinks.com",
            created_by=None,
        )
        recap = self._custom_recap(
            template_name="Torch THC-Event Activation",
            event_type_name="Event Activation",
            address="500 Congress Ave, Austin, TX 78701",
            request=req,
        )
        assert is_torch_portal_recap(recap) is True
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert emails == FIELD_MARKETING | {
            "events@igniteproductions.co",
            "nevena@igniteproductions.co",
        }

    def test_legacy_recap_on_event_activation_request_is_non_retail(self):
        event_activation = self.create_request_type(
            name="Event Activation", tenant=self.torch
        )
        req = em.Request.objects.create(
            name="Torch activation",
            address="1 Easton Way, Columbus, OH 43219",
            tenant=self.torch,
            status=self.req_approved,
            request_type=event_activation,
            created_by=None,
        )
        recap = self._recap(address="1 Easton Way, Columbus, OH 43219", request=req)
        assert torch_recap_execution_type(recap) == "event"
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert emails == FIELD_MARKETING | {
            "events@igniteproductions.co",
            "nevena@igniteproductions.co",
        }

    def test_retail_sampling_custom_recap_keeps_state_list(self):
        recap = self._custom_recap(
            template_name="Torch THC-Retail Sampling",
            event_type_name="Retail Sampling",
            address="500 Vine St, Cincinnati, OH 45202",
        )
        assert torch_recap_execution_type(recap) == "retail"
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert emails == {
            "john@torchdrinks.com",
            "doug@torchdrinks.com",
            "liberty@torchdrinks.com",
            "jason@torchdrinks.com",
        }

    def test_field_marketing_list_is_exactly_the_five(self):
        assert set(torch_field_marketing_recap_emails()) == FIELD_MARKETING
        weekly = set(torch_weekly_digest_emails())
        for email in ("alec@torchdrinks.com", "brittany@torchdrinks.com"):
            assert email not in weekly
        for state in (None, "FL", "OH", "TX", "GA"):
            retail = set(torch_retail_recap_emails(state))
            assert "ryanheuser@torchdrinks.com" not in retail
            assert not retail & (FIELD_MARKETING - {"ryanheuser@torchdrinks.com"})

    def test_portal_ohio_includes_jason_and_requestor(self):
        req = em.Request.objects.create(
            name="Torch portal OH demo",
            address="1 Easton Way, Columbus, OH 43219",
            tenant=self.torch,
            status=self.req_approved,
            request_type=self.request_type,
            requestor_email="buyer@store.com",
            created_by=None,
        )
        recap = self._recap(
            address="1 Easton Way, Columbus, OH 43219",
            request=req,
            name="Portal OH",
        )
        assert is_torch_portal_recap(recap) is True
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert "jason@torchdrinks.com" in emails
        assert "buyer@store.com" in emails
        assert "events@igniteproductions.co" in emails

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
        assert "collin@torchenterprise.com" in recipients
        assert "john@torchdrinks.com" in recipients
        assert "james@torchdrinks.com" in recipients

    def _run_digest(self, *args):
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
                has_content=True, completed_activations=1, upcoming_total=0
            )
            call_command("send_client_weekly_digest", f"--tenant={self.torch.id}", "--force", *args)
        return captured, build

    def test_only_email_sends_to_that_one_recipient_as_of(self):
        import datetime

        captured, build = self._run_digest(
            "--only-email=Collin@TorchEnterprise.com", "--as-of=2026-10-05T14:42:28Z"
        )
        assert len(captured) == 1
        assert captured[0]["recipients"] == ["collin@torchenterprise.com"]
        assert build.call_args[0][1] == datetime.datetime(
            2026, 10, 5, 14, 42, 28, tzinfo=datetime.timezone.utc
        )

    def test_only_email_not_on_list_sends_nothing(self):
        captured, _ = self._run_digest("--only-email=stray@torchdrinks.com")
        assert captured == []

    def test_only_email_dry_run_sends_nothing(self):
        captured, _ = self._run_digest("--only-email=collin@torchenterprise.com", "--dry-run")
        assert captured == []

    def test_only_email_requires_tenant(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command("send_client_weekly_digest", "--only-email=collin@torchenterprise.com")

    def test_collin_weekly_only_never_per_recap(self):
        assert "collin@torchenterprise.com" in torch_weekly_digest_emails()
        for state in (None, "FL", "OH", "TX", "GA"):
            assert "collin@torchenterprise.com" not in torch_retail_recap_emails(state)
        recap = self._recap(address="1 Easton Way, Columbus, OH 43219")
        emails = {e.lower() for e, _ in _collect_recap_approved_recipients(recap)[0]}
        assert "collin@torchenterprise.com" not in emails
