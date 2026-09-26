"""Unit tests for Torch sheet → event confirmation mapper / idempotency."""

from __future__ import annotations

from datetime import time
from unittest.mock import patch

import pytest

from events.sheet_event_confirmations import (
    STATUS_CANCELLED,
    STATUS_QUEUED,
    STATUS_SENT,
    SheetRowPayload,
    parse_sheet_clock,
    parse_sheet_date_iso,
    payload_from_mapping,
    products_from_skus_cell,
    resolve_timezone_for_row,
    send_from_sheet_row,
    cancel_from_sheet_row,
    validate_sheet_id,
    TORCH_PUBLIC_FORM_SHEET_ID,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1p", time(13, 0)),
        ("4p", time(16, 0)),
        ("10a", time(10, 0)),
        ("10:30a", time(10, 30)),
        ("12p", time(12, 0)),
        ("12a", time(0, 0)),
        ("13:00", time(13, 0)),
        ("9", time(9, 0)),
        (" 3:15p ", time(15, 15)),
    ],
)
def test_parse_sheet_clock(raw, expected):
    assert parse_sheet_clock(raw) == expected


def test_parse_sheet_clock_rejects_junk():
    with pytest.raises(ValueError):
        parse_sheet_clock("noonish")


@pytest.mark.parametrize(
    "raw",
    ["Sep 18, 2026", "09/18/2026", "2026-09-18", "September 18, 2026"],
)
def test_parse_sheet_date_iso(raw):
    assert parse_sheet_date_iso(raw).isoformat() == "2026-09-18"


def test_parse_sheet_date_iso_accepts_sept_with_a_comma():
    assert parse_sheet_date_iso("Sept, 25, 2026").isoformat() == "2026-09-25"


def test_products_from_skus_cell():
    assert products_from_skus_cell("Raspberry 10mg, Peach 5mg; Mango") == [
        "Raspberry 10mg",
        "Peach 5mg",
        "Mango",
    ]


def test_validate_sheet_id_hard_gate():
    assert validate_sheet_id(None) == TORCH_PUBLIC_FORM_SHEET_ID
    with pytest.raises(ValueError):
        validate_sheet_id("not-the-torch-sheet")


def test_validate_sheet_id_allows_liquid_death():
    from events.sheet_event_confirmations import LIQUID_DEATH_SHEET_ID

    assert validate_sheet_id(LIQUID_DEATH_SHEET_ID) == LIQUID_DEATH_SHEET_ID


def test_resolve_timezone_geocode_success():
    def fake_geocode(_address):
        return {"lat": 41.88, "lng": -87.63, "state": "Illinois", "properties": {}}

    iana, note = resolve_timezone_for_row(
        "123 N State St, Chicago, IL",
        geocode=fake_geocode,
    )
    assert iana == "America/Chicago"
    assert note == ""


def test_resolve_timezone_falls_back_to_state_column():
    def boom(_address):
        return None

    iana, note = resolve_timezone_for_row(
        "mystery address",
        state_hint="CA",
        geocode=boom,
    )
    assert iana == "America/Los_Angeles"
    assert "fallback" in note


def test_payload_from_mapping_reads_headers():
    payload = payload_from_mapping(
        {
            "Date": "Sep 18, 2026",
            "Start Time": "1p",
            "End Time": "4p",
            "Store Name": "Binny's",
            "Address": "123 Main, Chicago, IL",
            "State": "IL",
            "SKUs to sample": "Torch 10mg",
            "BA Name": "Alex BA",
            "Email": "alex@example.com",
            "Force Resend": True,
            "Confirmation Status": "Sent",
        },
        row_number=12,
    )
    assert payload.ba_email == "alex@example.com"
    assert payload.force_resend is True
    assert payload.confirmation_status == "Sent"


def test_send_idempotent_refuses_already_sent():
    payload = SheetRowPayload(
        row_number=5,
        confirmation_status=STATUS_SENT,
        ba_name="Alex",
        ba_email="alex@example.com",
        date="Sep 18, 2026",
        start_time="1p",
    )
    result = send_from_sheet_row(payload)
    assert result.ok is False
    assert "Already Sent" in result.message
    assert result.details.get("already_sent") is True


def test_send_after_cancelled_does_not_require_force_resend():
    """BA swap path: Cancel → new BA → Send (no Force Resend checkbox)."""
    payload = SheetRowPayload(
        row_number=231,
        confirmation_status=STATUS_CANCELLED,
        confirmation_uuid="01a0d8b1-c262-7dad-8eb2-2b0cb8c3ae2b",
        ba_name="Lois Brown",
        ba_email="loisshafer@hotmail.com",
        date="Sep 26, 2026",
        start_time="2p",
        end_time="5p",
        store_name="Total Wine & More (Alliance)",
        address="3101 Texas Sage Trail, Fort Worth, TX 76177",
        state="TX",
        dry_run=True,
    )
    fake_tenant = type("T", (), {"slug": "keee-torch-thc", "id": 17, "name": "Torch"})()
    with patch(
        "events.sheet_event_confirmations.resolve_timezone_for_row",
        return_value=("America/Chicago", ""),
    ), patch(
        "events.sheet_event_confirmations._tenant_for",
        return_value=fake_tenant,
    ):
        result = send_from_sheet_row(payload)
    assert result.ok is True
    assert result.dry_run is True
    assert "loisshafer@hotmail.com" in result.message


def test_send_when_ba_email_changed_bypasses_sent_guard():
    """Sent row whose BA Email was overwritten should email the new BA."""
    payload = SheetRowPayload(
        row_number=50,
        confirmation_status=STATUS_SENT,
        confirmation_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ba_name="Lois Brown",
        ba_email="loisshafer@hotmail.com",
        date="Sep 26, 2026",
        start_time="2p",
        end_time="5p",
        store_name="Store",
        address="3101 Texas Sage Trail, Fort Worth, TX 76177",
        state="TX",
        dry_run=True,
    )
    prior = type("C", (), {"ba_email": "oldba@example.com"})()
    fake_tenant = type("T", (), {"slug": "keee-torch-thc", "id": 17, "name": "Torch"})()
    with patch(
        "events.models.EventConfirmation.objects.filter"
    ) as filt, patch(
        "events.sheet_event_confirmations.resolve_timezone_for_row",
        return_value=("America/Chicago", ""),
    ), patch(
        "events.sheet_event_confirmations._tenant_for",
        return_value=fake_tenant,
    ):
        filt.return_value.only.return_value.first.return_value = prior
        result = send_from_sheet_row(payload)
    assert result.ok is True
    assert result.dry_run is True
    assert "loisshafer@hotmail.com" in result.message


def test_send_queued_finalizes_when_confirmation_already_mailed():
    """Stuck Queued after a real send: re-stamp Sent, never email again."""
    payload = SheetRowPayload(
        row_number=91,
        confirmation_status=STATUS_QUEUED,
        ba_name="Christina",
        ba_email="et76vargas@gmail.com",
        date="Sep 18, 2026",
        start_time="1p",
        address="1 Main, Chicago, IL",
        state="IL",
    )
    fake = type(
        "C",
        (),
        {
            "uuid": "11111111-1111-1111-1111-111111111111",
            "ba_email": "et76vargas@gmail.com",
            "timezone_name": "America/Chicago",
            "pk": 9,
        },
    )()
    with patch(
        "events.sheet_event_confirmations._find_mailed_confirmation",
        return_value=fake,
    ) as find, patch(
        "events.sheet_event_confirmations.write_row_status"
    ) as write, patch(
        "events.sheet_event_confirmations._create_and_send"
    ) as create:
        result = send_from_sheet_row(payload)
    find.assert_called_once()
    create.assert_not_called()
    assert write.called
    assert result.ok is True
    assert result.status == STATUS_SENT
    assert result.details.get("already_sent") is True
    assert "no new email" in result.message.lower()
    assert result.confirmation_uuid == str(fake.uuid)


def test_send_queued_refuses_without_mailed_confirmation():
    payload = SheetRowPayload(
        row_number=92,
        confirmation_status=STATUS_QUEUED,
        ba_name="Michelle",
        ba_email="orangedog_48@yahoo.com",
        date="Sep 18, 2026",
        start_time="1p",
    )
    with patch(
        "events.sheet_event_confirmations._find_mailed_confirmation",
        return_value=None,
    ), patch(
        "events.sheet_event_confirmations._create_and_send"
    ) as create:
        result = send_from_sheet_row(payload)
    create.assert_not_called()
    assert result.ok is False
    assert result.status == STATUS_QUEUED
    assert "Do not Force Resend" in result.message
    assert result.details.get("blocked") == "queued"


@pytest.mark.django_db(transaction=True)
def test_find_mailed_confirmation_matches_uuid_with_booked_send():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from django.contrib.auth import get_user_model
    from django.utils import timezone as dj_tz

    from events.models import EventConfirmation, EventConfirmationSend
    from events.sheet_event_confirmations import _find_mailed_confirmation
    from tenants.models import Tenant
    from tenants.tests.base import ensure_role

    chicago = ZoneInfo("America/Chicago")
    starts = datetime(2026, 9, 18, 13, 0, tzinfo=chicago)

    User = get_user_model()
    role = ensure_role("System")
    user = User.objects.filter(username="torch-sheet-test").first()
    if user is None:
        user = User.objects.create_user(
            username="torch-sheet-test",
            email="torch-sheet-test@spark.local",
            first_name="Torch",
            role=role,
            is_superuser=True,
            is_staff=True,
            is_active=True,
        )
    tenant = Tenant.objects.create(
        name="Torch Queued Finalize Test",
        slug="torch-queued-finalize-test",
        request_url_name="torch-queued-finalize-test",
        created_by=user,
    )
    conf = EventConfirmation.objects.create(
        tenant=tenant,
        ba_name="Christina",
        ba_email="et76vargas@gmail.com",
        store_name="Store",
        address="1 Main",
        event_type_label="Retail Sampling",
        starts_at=starts,
        timezone_name="America/Chicago",
        products=["Torch 10mg"],
        send_reminders=True,
    )
    EventConfirmationSend.objects.create(
        confirmation=conf,
        stage=EventConfirmation.STAGE_BOOKED,
        to_email=conf.ba_email,
        sent_at=dj_tz.now(),
        attempts=1,
    )
    payload = SheetRowPayload(
        row_number=91,
        confirmation_status=STATUS_QUEUED,
        confirmation_uuid=str(conf.uuid),
        ba_email="et76vargas@gmail.com",
        date="Sep 18, 2026",
        start_time="1p",
    )
    with patch(
        "events.sheet_event_confirmations._tenant_for",
        return_value=tenant,
    ):
        found = _find_mailed_confirmation(payload)
    assert found is not None
    assert found.pk == conf.pk


def test_send_force_resend_bypasses_sent_guard_then_hits_validation():
    """Force resend clears the Sent guard; missing BA name still fails."""
    payload = SheetRowPayload(
        row_number=5,
        confirmation_status=STATUS_SENT,
        force_resend=True,
        ba_name="",
        ba_email="alex@example.com",
        date="Sep 18, 2026",
        start_time="1p",
        address="1 Main, Chicago, IL",
        state="IL",
    )
    fake_tenant = type("T", (), {"slug": "keee-torch-thc", "id": 17, "name": "Torch"})()
    with patch(
        "events.sheet_event_confirmations.write_row_status"
    ), patch(
        "events.sheet_event_confirmations.resolve_timezone_for_row",
        return_value=("America/Chicago", ""),
    ), patch(
        "events.sheet_event_confirmations._tenant_for",
        return_value=fake_tenant,
    ):
        result = send_from_sheet_row(payload)
    assert result.ok is False
    assert result.status == "Error"
    assert "BA Name" in result.message


def test_cancel_without_prior_sent_skips_email():
    payload = SheetRowPayload(
        row_number=5,
        confirmation_status="",
        ba_name="Alex",
        ba_email="alex@example.com",
        date="Sep 18, 2026",
        start_time="1p",
    )
    with patch(
        "events.sheet_event_confirmations.write_row_status"
    ) as write, patch(
        "events.event_confirmations.send_cancellation_email"
    ) as mail:
        result = cancel_from_sheet_row(payload)
    assert result.ok is True
    assert result.status == STATUS_CANCELLED
    mail.assert_not_called()
    assert write.called


def test_cancel_dry_run_when_sent():
    payload = SheetRowPayload(
        row_number=5,
        confirmation_status=STATUS_SENT,
        ba_email="alex@example.com",
        dry_run=True,
    )
    result = cancel_from_sheet_row(payload)
    assert result.ok is True
    assert result.dry_run is True
    assert result.details.get("would_email") is True


def test_tomorrow_date_is_not_blocked():
    """Kyle: confirmations for tomorrow must be allowed (no date gate)."""
    from datetime import date, timedelta

    tomorrow = date.today() + timedelta(days=1)
    # parse accepts Sep-style; build via iso through mapping of M/D/YYYY
    parsed = parse_sheet_date_iso(tomorrow.strftime("%m/%d/%Y"))
    assert parsed == tomorrow


def test_known_status_cols_match_live_retail_layout():
    """Hot-path stamps must hit O/AB–AE without reading the header row."""
    from events.torch_sheet_confirmations import _KNOWN_STATUS_COLS, SENT_STATUS_HEADER

    assert _KNOWN_STATUS_COLS[SENT_STATUS_HEADER] == "O"
    assert _KNOWN_STATUS_COLS["Confirmation Status"] == "AB"
    assert _KNOWN_STATUS_COLS["Confirmation Sent At"] == "AC"
    assert _KNOWN_STATUS_COLS["Confirmation Error"] == "AD"
    assert _KNOWN_STATUS_COLS["Spark Confirmation UUID"] == "AE"


def test_write_row_status_uses_known_letters_not_header_read():
    from events.torch_sheet_confirmations import write_row_status

    class _FakeReq:
        def __init__(self, payload=None):
            self.payload = payload

        def execute(self):
            return {"ok": True}

    captured: dict = {}

    class _FakeValues:
        def batchUpdate(self, **kwargs):
            captured["batch"] = kwargs
            return _FakeReq()

    class _FakeSpreadsheets:
        def values(self):
            return _FakeValues()

        def get(self, **kwargs):
            raise AssertionError("write_row_status must not read sheet meta/headers")

    class _FakeSvc:
        def spreadsheets(self):
            return _FakeSpreadsheets()

    with patch(
        "events.sheet_event_confirmations._service", return_value=_FakeSvc()
    ), patch(
        "events.sheet_event_confirmations._ensure_confirmation_headers"
    ) as ensure:
        write_row_status(
            sheet_id=TORCH_PUBLIC_FORM_SHEET_ID,
            row_number=78,
            status=STATUS_SENT,
            confirmation_uuid="uuid-1",
            sent_at="2026-09-18 00:44 CDT",
            sent_column_value="Sent 2026-09-18 00:44 CDT",
        )
    ensure.assert_not_called()
    ranges = [d["range"] for d in captured["batch"]["body"]["data"]]
    assert "'Retail Schedule'!AB78" in ranges
    assert "'Retail Schedule'!AC78" in ranges
    assert "'Retail Schedule'!AD78" in ranges
    assert "'Retail Schedule'!AE78" in ranges
    assert "'Retail Schedule'!O78" in ranges


def test_resend_confirmation_column_is_not_sticky_force():
    """The one-shot column must not set force_resend. Only the request flag does."""
    payload = payload_from_mapping(
        {
            "Resend Confirmation": True,
            "Force Resend": False,
            "Confirmation Status": "Sent",
            "Email": "alex@example.com",
        },
        row_number=12,
    )
    assert payload.force_resend is False

    from events.sheet_event_confirmations import CONFIRMATION_EXTRA_HEADERS

    assert CONFIRMATION_EXTRA_HEADERS[-1] == "Resend Confirmation"


def test_handle_action_resend_flag_force_sends_once_then_stops():
    """resend=True bypasses Sent for this call only. The next send does not."""
    from events.sheet_event_confirmations import ActionResult, handle_action

    sent = ActionResult(
        ok=True,
        action="send",
        status=STATUS_SENT,
        message="Confirmation emailed to alex@example.com",
    )
    values = {
        "Confirmation Status": "Sent",
        "BA Name": "Alex BA",
        "Email": "alex@example.com",
        "Date": "Sep 18, 2026",
        "Start Time": "1p",
        "Address": "1 Main, Chicago, IL",
        "State": "IL",
        "Resend Confirmation": True,
        "Force Resend": False,
    }
    with (
        patch("events.sheet_event_confirmations.write_row_status"),
        patch(
            "events.sheet_event_confirmations.resolve_timezone_for_row",
            return_value=("America/Chicago", ""),
        ),
        patch(
            "events.sheet_event_confirmations._create_and_send",
            return_value=sent,
        ) as create,
    ):
        result = handle_action("send", values, row_number=12, resend=True)
    create.assert_called_once()
    assert create.call_args.args[0].force_resend is True
    assert result.ok is True

    with patch("events.sheet_event_confirmations._create_and_send") as again:
        blocked = handle_action("send", values, row_number=12, resend=False)
    again.assert_not_called()
    assert blocked.ok is False
    assert "Already Sent" in blocked.message


def test_view_passes_resend_flag_not_column(settings):
    import json

    from django.test import RequestFactory

    from events.sheet_event_confirmation_views import TorchSheetEventConfirmationView
    from events.sheet_event_confirmations import ActionResult

    settings.INTERNAL_CRON_SECRET = "test-secret"
    factory = RequestFactory()
    sent = ActionResult(
        ok=True, action="send", status=STATUS_SENT, message="ok"
    )

    def _post(resend, column):
        request = factory.post(
            "/internal/torch-sheet-event-confirmation",
            data=json.dumps(
                {
                    "action": "send",
                    "rowNumber": 12,
                    "resend": resend,
                    "values": {
                        "Confirmation Status": "Sent",
                        "Resend Confirmation": column,
                        "Force Resend": False,
                        "Email": "alex@example.com",
                    },
                }
            ),
            content_type="application/json",
            HTTP_X_CRON_SECRET="test-secret",
        )
        with patch(
            "events.sheet_event_confirmation_views.handle_action",
            return_value=sent,
        ) as handle:
            response = TorchSheetEventConfirmationView.as_view()(request)
        return response, handle

    response, handle = _post(True, False)
    assert response.status_code == 200
    assert handle.call_args.kwargs["resend"] is True

    _response, handle = _post(False, True)
    assert handle.call_args.kwargs["resend"] is False

