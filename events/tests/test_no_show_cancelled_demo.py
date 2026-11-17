"""The no-show nudge must not fire for a demo the client cancelled.

Cancelling a demo (events/demo_cancel.py) leaves its bookings approved, so
before this guard every BA on a cancelled demo got "you're not clocked in"
45 minutes after the start time.
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from ambassadors.models import AmbassadorEvent, PushDevice
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import models as event_models

PUSH = "ambassadors.push._send_push_to_user_sync"


@pytest.mark.django_db(transaction=True)
class TestNoShowSkipsCancelledDemos(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="No Show Brand")
        self.now = timezone.now()
        self.bookings = []
        for i in range(2):
            user = self.create_user(
                username=f"noshow{i}@test.com",
                email=f"noshow{i}@test.com",
                role=self.roles["ambassador"],
            )
            PushDevice.objects.create(user=user, token=f"tok-{i}", platform="ios")
            ba = self.create_ambassador(user)
            event = self.create_event(
                name=f"Store {i}",
                tenant=self.tenant,
                address="1 Main St",
                date=self.now - timedelta(hours=1),
            )
            event_models.Event.objects.filter(pk=event.pk).update(
                start_time=self.now - timedelta(hours=1),
                end_time=self.now + timedelta(hours=2),
            )
            self.bookings.append(
                AmbassadorEvent.objects.create(
                    ambassador=ba,
                    event=event,
                    tenant=self.tenant,
                    is_approved=True,
                    created_by=self.get_system_user(),
                )
            )

    def test_only_the_live_demo_gets_a_nudge(self):
        live, cancelled = self.bookings
        event_models.Event.objects.filter(pk=cancelled.event_id).update(
            status=self.create_event_status(
                name="Suspended", tenant=self.tenant, slug="suspended"
            )
        )
        with patch(PUSH, return_value=1) as push:
            call_command("send_no_show_alerts", stdout=StringIO())

        assert push.call_count == 1
        assert push.call_args.args[0] == live.ambassador.user_id
        live.refresh_from_db()
        cancelled.refresh_from_db()
        assert live.no_show_alerted_at is not None
        assert cancelled.no_show_alerted_at is None

    def test_a_declined_request_also_stops_the_nudge(self):
        live, cancelled = self.bookings
        request = event_models.Request.objects.create(
            name="Declined demo",
            address="1 Main St",
            tenant=self.tenant,
            status=self.create_request_status(
                name="Declined", tenant=self.tenant, slug="declined"
            ),
            request_type=self.create_request_type(name="Retail", tenant=self.tenant),
            created_by=None,
        )
        event_models.Event.objects.filter(pk=cancelled.event_id).update(request=request)
        with patch(PUSH, return_value=1) as push:
            call_command("send_no_show_alerts", stdout=StringIO())
        assert push.call_count == 1
        assert push.call_args.args[0] == live.ambassador.user_id
