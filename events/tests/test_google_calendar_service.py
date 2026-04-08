from types import SimpleNamespace
from datetime import date, time

import pytest

from tenants.calendar.service import GoogleCalendarService


@pytest.mark.django_db
class TestGoogleCalendarService:
    def test_format_event_data_uses_event_offset_and_event_timezone(self):
        service = GoogleCalendarService(user=SimpleNamespace(id=1))
        event_timezone = SimpleNamespace(
            code="CST",
            name="Central-CST",
            offset=-360,
        )
        event = SimpleNamespace(
            id=123,
            name="Test Event",
            notes="Notes",
            timezone=event_timezone,
            request=None,
            date=date(2026, 4, 30),
            start_time=time(15, 0),
            end_time=time(17, 0),
            address="123 Test St",
        )

        event_data = service._format_event_data(event)

        assert event_data["start"]["dateTime"] == "2026-04-30T15:00:00-06:00"
        assert event_data["end"]["dateTime"] == "2026-04-30T17:00:00-06:00"
        assert event_data["start"]["timeZone"] == "America/Chicago"
        assert event_data["end"]["timeZone"] == "America/Chicago"

    def test_format_event_data_without_timezone_uses_utc_offset(self):
        service = GoogleCalendarService(user=SimpleNamespace(id=1))
        event = SimpleNamespace(
            id=124,
            name="UTC Event",
            notes=None,
            timezone=None,
            request=None,
            date=date(2026, 4, 30),
            start_time=time(15, 0),
            end_time=time(17, 0),
            address=None,
        )

        event_data = service._format_event_data(event)

        assert event_data["start"]["dateTime"] == "2026-04-30T15:00:00+00:00"
        assert event_data["end"]["dateTime"] == "2026-04-30T17:00:00+00:00"
        assert "timeZone" not in event_data["start"]
        assert "timeZone" not in event_data["end"]
