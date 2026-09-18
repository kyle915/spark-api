"""Liquid Death sheet → event confirmation allowlist / column layout."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from events.sheet_event_confirmations import (
    LIQUID_DEATH_SHEET_ID,
    SENT_STATUS_HEADER,
    STATUS_SENT,
    SheetRowPayload,
    config_for_sheet_id,
    payload_from_mapping,
    send_from_sheet_row,
    validate_sheet_id,
    write_row_status,
)


def test_ld_config_allowlisted():
    cfg = config_for_sheet_id(LIQUID_DEATH_SHEET_ID)
    assert cfg.key == "liquid-death"
    assert cfg.tenant_slug == "liquid-death"
    assert cfg.event_type_label == "Retail Sampling"
    assert SENT_STATUS_HEADER in cfg.extra_headers
    assert "Resend Confirmation" in cfg.extra_headers
    assert cfg.extra_headers[-1] == "Resend Confirmation"
    assert "Resend Confirmation" not in cfg.known_status_cols
    assert cfg.known_status_cols[SENT_STATUS_HEADER] == "AB"
    assert cfg.known_status_cols["Confirmation Status"] == "AF"
    assert cfg.known_status_cols["Spark Confirmation UUID"] == "AI"


def test_ld_payload_from_mapping():
    payload = payload_from_mapping(
        {
            "Date": "10/3/2026",
            "Start Time": "10a",
            "End Time": "4p",
            "Store Name": "Piggly Wiggly #277",
            "Address": "W189S7847 Racine Ave, Muskego, WI 53150, USA",
            "State": "WI",
            "SKUs to sample": "Doctor Death, Rootbeer Wrath",
            "BA Name": "Alex BA",
            "Email": "alex@example.com",
        },
        row_number=3,
        sheet_id=LIQUID_DEATH_SHEET_ID,
    )
    assert payload.sheet_id == LIQUID_DEATH_SHEET_ID
    assert payload.store_name.startswith("Piggly")
    assert payload.ba_email == "alex@example.com"


def test_ld_send_refuses_already_sent():
    payload = SheetRowPayload(
        row_number=5,
        sheet_id=LIQUID_DEATH_SHEET_ID,
        confirmation_status=STATUS_SENT,
        ba_name="Alex",
        ba_email="alex@example.com",
        date="10/3/2026",
        start_time="10a",
    )
    result = send_from_sheet_row(payload)
    assert result.ok is False
    assert "Already Sent" in result.message


def test_ld_write_row_status_uses_ab_ai_letters():
    captured: dict = {}

    class _FakeReq:
        def execute(self):
            return {"ok": True}

    class _FakeValues:
        def batchUpdate(self, **kwargs):
            captured["batch"] = kwargs
            return _FakeReq()

    class _FakeSpreadsheets:
        def values(self):
            return _FakeValues()

        def get(self, **kwargs):
            class R:
                def execute(self_inner):
                    return {
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 0,
                                    "title": "Retail Market Schedule",
                                    "index": 0,
                                }
                            }
                        ]
                    }

            return R()

    class _FakeSvc:
        def spreadsheets(self):
            return _FakeSpreadsheets()

    with patch(
        "events.sheet_event_confirmations._service", return_value=_FakeSvc()
    ):
        write_row_status(
            sheet_id=LIQUID_DEATH_SHEET_ID,
            row_number=12,
            status=STATUS_SENT,
            confirmation_uuid="uuid-ld",
            sent_at="2026-09-18 12:00 CDT",
            sent_column_value="Sent 2026-09-18 12:00 CDT",
        )
    ranges = [d["range"] for d in captured["batch"]["body"]["data"]]
    assert any("AB12" in r for r in ranges)
    assert any("AF12" in r for r in ranges)
    assert any("AI12" in r for r in ranges)
    assert all("O12" not in r for r in ranges)


def test_validate_rejects_random_sheet():
    with pytest.raises(ValueError):
        validate_sheet_id("totally-fake-sheet-id")
