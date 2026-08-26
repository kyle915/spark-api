"""Public Torch spark-form → retail-schedule Sheet mapping."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from utils.torch_public_form_sheet import (
    SPARK_EXTRA_HEADERS,
    TORCH_PUBLIC_FORM_SHEET_ID,
    UUID_HEADER,
    append_torch_public_form_row,
    append_torch_request_row,
    _insert_index_for_date,
    _parse_sheet_date,
    build_torch_public_form_values,
    extract_city,
    should_append_torch_public_form,
    _ensure_extra_headers,
    _row_from_values,
)

UTC = dt.timezone.utc
EASTERN = SimpleNamespace(name="Eastern", code="EST", offset=-300)


def _products(*names: str):
    rows = []
    for name in names:
        rows.append(SimpleNamespace(product=SimpleNamespace(name=name)))
    return SimpleNamespace(all=lambda: rows)


def _request(**extra):
    base = dict(
        id=1980,
        uuid=UUID("0198c0a0-0000-7000-8000-000000001980"),
        name="IN AND OUT LIQUORS",
        retailer_name="IN AND OUT LIQUORS",
        retailer=None,
        address="1734 N Harbor City Blvd, Melbourne, FL 32935, USA",
        date=dt.datetime(2026, 9, 11, 16, 0, tzinfo=UTC),
        start_time=dt.datetime(2026, 9, 11, 21, 0, tzinfo=UTC),
        end_time=dt.datetime(2026, 9, 12, 0, 0, tzinfo=UTC),
        timezone=EASTERN,
        state=SimpleNamespace(code="FL"),
        request_type=SimpleNamespace(name="Retail Sampling"),
        notes="BA count: 2",
        is_non_active_product_required=False,
        cases_to_be_shipped="6",
        client_name="LeslyAnn Altet",
        requestor_email="leslyann@example.com",
        client_email="leslyann@example.com",
        store_manager_phone="",
        scheduling_status="already_scheduled",
        created_at=dt.datetime(2026, 8, 20, 16, 33, tzinfo=UTC),
        tenant=SimpleNamespace(
            slug="torch-thc", name="Torch THC", request_url_name="keee-torch-thc"
        ),
        request_product=_products(
            "Fruit Punch 25mg 12oz",
            "Pink Lemonade 25mg 12oz",
            "Blue Razz Lemonade 60mg 12oz",
            "Melon Madness 60mg 12oz",
        ),
    )
    base.update(extra)
    return SimpleNamespace(**base)


def test_gate_torch_public_form_only():
    torch = SimpleNamespace(
        slug="torch-thc", name="Torch THC", request_url_name="keee-torch-thc"
    )
    ld = SimpleNamespace(
        slug="liquid-death", name="Liquid Death", request_url_name="ighn-liquid-death"
    )
    assert should_append_torch_public_form("keee-torch-thc", torch) is True
    assert should_append_torch_public_form("ighn-liquid-death", ld) is False
    assert should_append_torch_public_form("keee-torch-thc", ld) is False
    assert should_append_torch_public_form("ighn-liquid-death", torch) is False


def test_extract_city_from_places_address():
    assert (
        extract_city("1734 N Harbor City Blvd, Melbourne, FL 32935, USA")
        == "Melbourne"
    )
    assert (
        extract_city("2200 Americana Blvd suite 6, Orlando, FL 32839, USA")
        == "Orlando"
    )


def test_row_maps_retail_schedule_and_spark_fields():
    values = build_torch_public_form_values(_request())
    assert values["State"] == "FL"
    assert values["Day of Week"] == "Friday"
    assert values["Date"] == "Sep 11, 2026"
    assert values["Store Name"] == "IN AND OUT LIQUORS"
    assert values["Start Time"] == "5p"
    assert values["End Time"] == "8p"
    assert values["Address"].startswith("1734 N Harbor City Blvd")
    assert values["Requested? "] == "Y"
    assert "Fruit Punch 25mg 12oz" in values["SKUs to sample"]
    assert "Melon Madness 60mg 12oz" in values["SKUs to sample"]
    assert "Rate" not in values
    assert values["BA Count"] == "2"
    assert values["Non-Active"] == "No"
    assert values["Cases to Ship"] == "6"
    assert values["Requestor Name"] == "LeslyAnn Altet"
    assert values["Requestor Email"] == "leslyann@example.com"
    assert values["Schedule With"] == "Schedule with account"
    assert values["City"] == "Melbourne"
    assert values["Request ID"] == "REQ-1980"
    assert values["Request Type"] == "Retail Sampling"
    assert str(values[UUID_HEADER]).endswith("1980")
    assert "/request/view/" in values["Spark Link"]


def test_row_schedule_for_me_and_non_active_yes():
    values = build_torch_public_form_values(
        _request(
            scheduling_status="needs_scheduling",
            is_non_active_product_required=True,
            cases_to_be_shipped="12",
        )
    )
    assert values["Schedule With"] == "Schedule for me"
    assert values["Non-Active"] == "Yes"
    assert values["Cases to Ship"] == "12"


def test_row_from_values_keeps_rate_blank():
    header = [
        "State",
        "Store Name",
        "Rate",
        "BA Name",
        UUID_HEADER,
        "Requestor Email",
    ]
    values = {
        "State": "FL",
        "Store Name": "Party liquor",
        UUID_HEADER: "abc",
        "Requestor Email": "a@b.com",
    }
    row = _row_from_values(header, values)
    assert row[0] == "FL"
    assert row[1] == "Party liquor"
    assert row[2] == ""
    assert row[3] == ""
    assert row[4] == "abc"
    assert row[5] == "a@b.com"


def test_ensure_extra_headers_appends_without_rewriting_existing():
    existing = [
        "State",
        "Day of Week",
        "Date",
        "Store Name",
        "Start Time",
        "End Time",
        "Address",
        "Requested? ",
        "Notes",
        "SKUs to sample",
        "Rate",
        "Kyle Check",
    ]
    svc = MagicMock()
    values = svc.spreadsheets.return_value.values.return_value
    values.get.return_value.execute.return_value = {"values": [existing]}

    header = _ensure_extra_headers(svc, "sid", "Retail Schedule")
    update = values.update
    update.assert_called_once()
    kwargs = update.call_args.kwargs
    assert kwargs["range"] == "'Retail Schedule'!M1:X1"
    assert kwargs["body"]["values"] == [SPARK_EXTRA_HEADERS]
    assert header[: len(existing)] == existing
    assert header[-len(SPARK_EXTRA_HEADERS) :] == SPARK_EXTRA_HEADERS


def test_ensure_extra_headers_skips_when_already_present():
    existing = ["State", "Rate"] + SPARK_EXTRA_HEADERS
    svc = MagicMock()
    values = svc.spreadsheets.return_value.values.return_value
    values.get.return_value.execute.return_value = {"values": [existing]}
    header = _ensure_extra_headers(svc, "sid", None)
    values.update.assert_not_called()
    assert header == existing


def test_append_skips_non_torch_without_sheets_call():
    ld = _request(
        tenant=SimpleNamespace(
            slug="liquid-death",
            name="Liquid Death",
            request_url_name="ighn-liquid-death",
        )
    )
    with patch("utils.torch_public_form_sheet._service") as svc:
        assert append_torch_public_form_row(ld, "ighn-liquid-death") is False
        svc.assert_not_called()


def test_append_writes_new_row_and_is_idempotent_on_uuid():
    req = _request()
    svc = MagicMock()
    values_api = svc.spreadsheets.return_value.values.return_value
    header = ["State", "Store Name", "Rate"] + SPARK_EXTRA_HEADERS
    values_api.get.return_value.execute.side_effect = [
        {"values": [header]},
        {"values": []},
    ]
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Retail Schedule", "sheetId": 0}}]
    }

    with patch("utils.torch_public_form_sheet._service", return_value=svc):
        assert append_torch_public_form_row(req, "keee-torch-thc") is True

    append = values_api.append
    append.assert_called_once()
    body_row = append.call_args.kwargs["body"]["values"][0]
    assert body_row[0] == "FL"
    assert body_row[1] == "IN AND OUT LIQUORS"
    assert body_row[2] == ""
    assert str(body_row[3]).endswith("1980")
    assert append.call_args.kwargs["spreadsheetId"] == TORCH_PUBLIC_FORM_SHEET_ID

    values_api.get.return_value.execute.side_effect = [
        {"values": [header]},
        {"values": [[str(req.uuid)]]},
    ]
    values_api.append.reset_mock()
    with patch("utils.torch_public_form_sheet._service", return_value=svc):
        assert append_torch_public_form_row(req, "keee-torch-thc") is False
    values_api.append.assert_not_called()


def test_append_failure_is_swallowed():
    req = _request()
    with patch(
        "utils.torch_public_form_sheet._service",
        side_effect=RuntimeError("sheets down"),
    ):
        assert append_torch_public_form_row(req, "keee-torch-thc") is False


def test_signed_in_torch_request_appends_without_a_form_slug():
    """The in-app form has no slug, so this path is gated on tenant alone."""
    req = _request()
    svc = MagicMock()
    values_api = svc.spreadsheets.return_value.values.return_value
    header = ["State", "Store Name", "Rate"] + SPARK_EXTRA_HEADERS
    values_api.get.return_value.execute.side_effect = [
        {"values": [header]},
        {"values": []},
    ]
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Retail Schedule", "sheetId": 0}}]
    }

    with patch("utils.torch_public_form_sheet._service", return_value=svc):
        assert append_torch_request_row(req) is True
    values_api.append.assert_called_once()


def test_signed_in_non_torch_request_never_touches_sheets():
    """Tenant gate still holds when there is no slug to fall back on."""
    ld = _request(
        tenant=SimpleNamespace(
            slug="liquid-death",
            name="Liquid Death",
            request_url_name="ighn-liquid-death",
        )
    )
    with patch("utils.torch_public_form_sheet._service") as svc:
        assert append_torch_request_row(ld) is False
        svc.assert_not_called()


def _d(text):
    import datetime as _dt

    return _dt.date.fromisoformat(text)


def test_insert_index_places_a_date_between_its_neighbours():
    rows = [(2, _d("2026-08-26")), (3, _d("2026-09-15")), (4, _d("2026-12-27"))]
    assert _insert_index_for_date(rows, _d("2026-09-01")) == 3
    assert _insert_index_for_date(rows, _d("2026-01-01")) == 2
    # later than everything -> None means "append at the end"
    assert _insert_index_for_date(rows, _d("2027-01-05")) is None


def test_insert_index_refuses_to_guess_in_an_unsorted_sheet():
    """No position is correct in an unsorted list; scattering rows through the
    client's data is worse than appending."""
    unsorted_rows = [(2, _d("2026-09-15")), (3, _d("2026-08-26"))]
    assert _insert_index_for_date(unsorted_rows, _d("2026-09-01")) is None
    assert _insert_index_for_date([], _d("2026-09-01")) is None
    assert _insert_index_for_date([(2, _d("2026-08-26"))], None) is None


def test_sheet_date_parsing_never_guesses():
    assert _parse_sheet_date("09/12/2026") == _d("2026-09-12")
    assert _parse_sheet_date("2026-09-12") == _d("2026-09-12")
    assert _parse_sheet_date("Sep 12, 2026") == _d("2026-09-12")
    assert _parse_sheet_date("not a date") is None
    assert _parse_sheet_date("") is None


def test_reflow_anchors_on_client_rows_not_on_other_spark_rows():
    """A misplaced Spark row must be positioned against the CLIENT's sorted
    rows. Measuring against every other dated row means two stranded Spark
    rows make the list unsorted, the index declines to guess, and both just
    trade places at the bottom forever."""
    anchors = [(5, _d("2026-08-26")), (900, _d("2026-09-15")),
               (2140, _d("2026-12-27"))]
    assert _insert_index_for_date(anchors, _d("2026-09-12")) == 900
    assert _insert_index_for_date(anchors, _d("2026-09-18")) == 2140

    # Include another stranded Spark row and the list is no longer sorted,
    # so no position is offered — the shape that caused the bug.
    mixed = anchors + [(2142, _d("2026-09-18"))]
    assert _insert_index_for_date(mixed, _d("2026-09-12")) is None
