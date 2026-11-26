import datetime
from types import SimpleNamespace

from events.batch_requests import _local_datetime_to_utc


class TestBatchRequestLocalDatetimeToUtc:
    def test_uses_dst_aware_offset_in_summer_eastern(self):
        # Even if the TimeZone row stores the standard offset (-5), a July date
        # in America/New_York is EDT (UTC-4), so 12:00 local -> 16:00 UTC.
        tz_obj = SimpleNamespace(name="Eastern Standard Time", code="EST", offset=-5)
        result = _local_datetime_to_utc(
            datetime.datetime(2026, 7, 15, 12, 0),
            tz_obj.offset,
            timezone_obj=tz_obj,
        )
        assert result == datetime.datetime(2026, 7, 15, 16, 0, tzinfo=datetime.timezone.utc)

    def test_uses_standard_offset_in_winter_eastern(self):
        tz_obj = SimpleNamespace(name="Eastern Daylight Time", code="EDT", offset=-4)
        result = _local_datetime_to_utc(
            datetime.datetime(2026, 1, 15, 12, 0),
            tz_obj.offset,
            timezone_obj=tz_obj,
        )
        assert result == datetime.datetime(2026, 1, 15, 17, 0, tzinfo=datetime.timezone.utc)

    def test_uses_dst_aware_offset_in_summer_pacific(self):
        tz_obj = SimpleNamespace(name="Pacific Standard Time", code="PST", offset=-8)
        result = _local_datetime_to_utc(
            datetime.datetime(2026, 7, 15, 12, 0),
            tz_obj.offset,
            timezone_obj=tz_obj,
        )
        assert result == datetime.datetime(2026, 7, 15, 19, 0, tzinfo=datetime.timezone.utc)

    def test_falls_back_to_static_offset_when_timezone_obj_missing(self):
        result = _local_datetime_to_utc(
            datetime.datetime(2026, 7, 15, 12, 0),
            -6,
            timezone_obj=None,
        )
        assert result == datetime.datetime(2026, 7, 15, 18, 0, tzinfo=datetime.timezone.utc)
