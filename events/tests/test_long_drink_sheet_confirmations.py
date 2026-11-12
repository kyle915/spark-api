"""Long Drink EVENT CALENDAR tab → Mark Anthony Brands confirmation."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from events.event_confirmations import checkin_url_for, email_training_resources
from events.sheet_event_confirmations import (
    LONG_DRINK_SHEET_ID,
    LONG_DRINK_TAB_GID,
    MAB_CHECKIN_CODE,
    MAB_TENANT_SLUG,
    STATUS_SENT,
    SheetRowPayload,
    _derive_end_clock,
    config_for_sheet_id,
    payload_from_mapping,
    send_from_sheet_row,
    validate_sheet_id,
)
from tenants.models import Tenant
from tenants.tests.base import ensure_role


def _system_user():
    User = get_user_model()
    existing = User.objects.filter(username="system").first()
    if existing:
        return existing
    role = ensure_role("System")
    user = User.objects.create_user(
        username="system",
        email="system@spark.local",
        first_name="System",
        role=role,
        is_superuser=True,
        is_staff=True,
        is_active=True,
    )
    if not role.created_by:
        role.created_by = user
        role.save()
    return user


def _mab_tenant(*, checkin_code: str = MAB_CHECKIN_CODE, resources=None):
    user = _system_user()
    holder = Tenant.objects.filter(checkin_code=checkin_code).first()
    if holder is not None and holder.slug != MAB_TENANT_SLUG:
        holder.checkin_code = None
        holder.save(update_fields=["checkin_code"])
    tenant = Tenant.objects.filter(slug=MAB_TENANT_SLUG).order_by("id").first()
    if tenant is None:
        tenant = Tenant.objects.create(
            name="Mark Anthony Brands",
            slug=MAB_TENANT_SLUG,
            checkin_code=checkin_code,
            checkin_resources=resources,
            created_by=user,
        )
    else:
        tenant.checkin_code = checkin_code
        tenant.checkin_resources = resources
        tenant.save(update_fields=["checkin_code", "checkin_resources"])
    return tenant


def test_long_drink_config_allowlisted():
    cfg = config_for_sheet_id(LONG_DRINK_SHEET_ID)
    assert cfg.key == "long-drink"
    assert cfg.tenant_slug == MAB_TENANT_SLUG
    assert cfg.tab_gid == LONG_DRINK_TAB_GID
    assert cfg.tab_title == "Long Drink"
    assert cfg.event_type_label == "Retail Sampling"
    assert cfg.standing_checkin_code == MAB_CHECKIN_CODE
    assert "Resend Confirmation" in cfg.extra_headers
    assert cfg.known_status_cols["Confirmation Status"] == "U"
    assert cfg.known_status_cols["Spark Confirmation UUID"] == "X"


def test_long_drink_payload_maps_city_time_fixed_three_hours():
    # Sheet Hours may say 4 (pay column) — confirmation window is always 3h.
    payload = payload_from_mapping(
        {
            "City": "Rochester Hills",
            "Address": "1242 S Rochester Rd, Rochester Hills, MI 48307",
            "State": "MI",
            "Date": "11/6/2026",
            "Time": "4:00 PM",
            "Hours": "4",
            "BA Name": "Adriel Oliver",
            "Email": "adriel@example.com",
        },
        row_number=2,
        sheet_id=LONG_DRINK_SHEET_ID,
    )
    assert payload.sheet_id == LONG_DRINK_SHEET_ID
    assert payload.store_name == "Rochester Hills"
    assert payload.start_time == "4:00 PM"
    assert payload.end_time == "7p"
    assert payload.ba_email == "adriel@example.com"
    assert payload.state == "MI"


def test_derive_end_clock_half_hours():
    assert _derive_end_clock("12:00 PM", "3") == "3p"
    assert _derive_end_clock("3:00 PM", "3.5") == "6:30p"
    assert _derive_end_clock("", "4") == ""
    assert _derive_end_clock("4:00 PM", "") == ""


def test_long_drink_send_refuses_already_sent():
    payload = SheetRowPayload(
        row_number=5,
        sheet_id=LONG_DRINK_SHEET_ID,
        confirmation_status=STATUS_SENT,
        ba_name="Alex",
        ba_email="alex@example.com",
        date="11/6/2026",
        start_time="4:00 PM",
    )
    result = send_from_sheet_row(payload)
    assert result.ok is False
    assert "Already Sent" in result.message


@pytest.mark.django_db
def test_long_drink_dry_run_requires_standing_mab_code():
    _mab_tenant(checkin_code="MAB-WRONG1")
    payload = payload_from_mapping(
        {
            "City": "Conroe",
            "Address": "219 S Loop 336 W Conroe, TX 77304",
            "State": "TX",
            "Date": "11/13/2026",
            "Time": "4:00 PM",
            "Hours": "3",
            "BA Name": "Candace",
            "Email": "candace@example.com",
        },
        row_number=5,
        sheet_id=LONG_DRINK_SHEET_ID,
        dry_run=True,
    )
    with patch(
        "events.sheet_event_confirmations.resolve_timezone_for_row",
        return_value=("America/Chicago", ""),
    ):
        result = send_from_sheet_row(payload)
    assert result.ok is False
    assert "MAB-V696HM" in result.message


@pytest.mark.django_db
def test_long_drink_dry_run_ok_with_standing_code():
    tenant = _mab_tenant()
    payload = payload_from_mapping(
        {
            "City": "Lewisville",
            "Address": "2325 S Stemmons Fwy, Lewisville, TX 75067",
            "State": "TX",
            "Date": "11/7/2026",
            "Time": "3:00 PM",
            "Hours": "4",
            "BA Name": "Dan",
            "Email": "dan@example.com",
        },
        row_number=4,
        sheet_id=LONG_DRINK_SHEET_ID,
        dry_run=True,
    )
    with patch(
        "events.sheet_event_confirmations.resolve_timezone_for_row",
        return_value=("America/Chicago", ""),
    ):
        result = send_from_sheet_row(payload)
    assert result.ok is True
    assert result.dry_run is True
    assert "dan@example.com" in result.message
    assert result.details["checkin_urls"] == [checkin_url_for(tenant)]


@pytest.mark.django_db
def test_mab_email_includes_long_drink_guide():
    tenant = _mab_tenant(resources=None)
    resources = email_training_resources(tenant)
    assert len(resources) == 1
    assert resources[0]["label"] == "Long Drink BA Sampling Guide"
    assert resources[0]["url"].endswith(
        "/training/mark-anthony-brands/ba-training-guide.pdf"
    )


def test_validate_long_drink_sheet_id():
    assert validate_sheet_id(LONG_DRINK_SHEET_ID) == LONG_DRINK_SHEET_ID
    with pytest.raises(ValueError, match="unknown sheet"):
        validate_sheet_id("not-a-real-sheet-id")
