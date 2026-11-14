"""
Tests for the daily `send_recap_reminders` sweep.

It re-nudges every BA whose approved shift ended in the last week and who
has not FILED a recap for it. "Filed" is the shared definition in
recaps/filed.py and covers both recap families: a BA who filed a CustomRecap
must not be told their recap is overdue, and the empty stub a clock-out
leaves behind must not excuse anyone.

The push sender is stubbed for the whole test, as in test_send_recap_nudges.
"""

import io
from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

import pytest
from django.core.management import call_command

from ambassadors.models import AmbassadorEvent, PushDevice
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps.models import CustomRecap, CustomRecapTemplate, Recap


@pytest.mark.django_db(transaction=True)
class TestSendRecapReminders(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Reminder Sweep Tenant")
        self.admin = self.create_user(
            username="admin-reminders",
            email="admin-reminders@test.com",
            role=self.roles["spark_admin"],
        )
        self.ba_user, self.ambassador = self._ba("ba-reminders")
        with patch(
            "ambassadors.push._send_push_to_user_sync", return_value=1
        ) as mock_send:
            self.mock_send = mock_send
            yield

    def _ba(self, username):
        user = self.create_user(
            username=username,
            email=f"{username}@test.com",
            role=self.roles["ambassador"],
        )
        PushDevice.objects.create(
            user=user,
            token=f"ExponentPushToken[{username}]",
            platform="android",
        )
        return user, self.create_ambassador(user)

    def _shift_ended_days_ago(self, days, *, name="Ended shift"):
        end = datetime.now(_tz.utc) - timedelta(days=days, hours=3)
        event = self.create_event(
            name=name,
            tenant=self.tenant,
            date=end,
            start_time=end - timedelta(hours=4),
            end_time=end,
        )
        self._book(self.ambassador, event)
        return event

    def _book(self, ambassador, event):
        return AmbassadorEvent.objects.create(
            ambassador=ambassador,
            event=event,
            tenant=self.tenant,
            is_approved=True,
            created_by=self.admin,
        )

    def _custom_recap(self, event, ambassador, **kwargs):
        template = CustomRecapTemplate.objects.create(
            name=f"Tmpl {event.id}-{ambassador.id}",
            event_type=self.create_event_type(
                f"Sampling {event.id}-{ambassador.id}", self.tenant
            ),
            tenant=self.tenant,
            created_by=self.admin,
        )
        return CustomRecap.objects.create(
            name="custom recap",
            event=event,
            ambassador=ambassador,
            tenant=self.tenant,
            custom_recap_template=template,
            created_by=self.admin,
            **kwargs,
        )

    def _run(self, *args):
        self.mock_send.reset_mock()
        out = io.StringIO()
        call_command("send_recap_reminders", *args, stdout=out)
        return out.getvalue()

    def _pushed_user_ids(self):
        return sorted(call.args[0] for call in self.mock_send.call_args_list)

    def test_unfiled_shift_is_reminded_with_days_overdue(self):
        self._shift_ended_days_ago(3)
        self._run()
        self.mock_send.assert_called_once()
        assert "3 days overdue" in self.mock_send.call_args.kwargs["body"]

    def test_filed_custom_recap_stops_the_reminder(self):
        event = self._shift_ended_days_ago(3, name="Filed custom")
        self._custom_recap(
            event, self.ambassador, submitted_at=datetime.now(_tz.utc)
        )
        out = self._run()
        self.mock_send.assert_not_called()
        assert "skipped 1 already-filed" in out

    def test_filed_legacy_recap_stops_the_reminder(self):
        event = self._shift_ended_days_ago(3, name="Filed legacy")
        Recap.objects.create(
            name="filed",
            event=event,
            ambassador=self.ambassador,
            submited_at=datetime.now(_tz.utc),
            created_by=self.admin,
        )
        self._run()
        self.mock_send.assert_not_called()

    def test_empty_clock_out_stub_does_not_stop_the_reminder(self):
        event = self._shift_ended_days_ago(3, name="Stub only")
        self._custom_recap(event, self.ambassador)  # nothing in it
        Recap.objects.create(
            name="stub",
            event=event,
            ambassador=self.ambassador,
            created_by=self.admin,
        )
        self._run()
        self.mock_send.assert_called_once()

    def test_a_teammates_filing_does_not_excuse_the_other_ba(self):
        event = self._shift_ended_days_ago(3, name="Two BAs")
        mate_user, mate = self._ba("ba-reminders-mate")
        self._book(mate, event)
        self._custom_recap(event, mate, submitted_at=datetime.now(_tz.utc))
        self._run()
        assert self._pushed_user_ids() == [self.ba_user.id]
