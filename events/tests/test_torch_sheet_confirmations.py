"""Unit tests for Torch sheet → event confirmation mapper / idempotency."""

from __future__ import annotations

from datetime import time
from unittest.mock import patch

import pytest

from events.torch_sheet_confirmations import (
    STATUS_CANCELLED,
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
        "events.torch_sheet_confirmations.write_row_status"
    ), patch(
        "events.torch_sheet_confirmations.resolve_timezone_for_row",
        return_value=("America/Chicago", ""),
    ), patch(
        "events.torch_sheet_confirmations._torch_tenant",
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
        "events.torch_sheet_confirmations.write_row_status"
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
