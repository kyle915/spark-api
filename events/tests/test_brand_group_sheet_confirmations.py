"""Famus / Alcohol Armor / Wook Water sheet confirmations.

Column H picks the brands. The email is the Torch confirmation template.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from events.brand_group_sheet import (
    BRAND_GROUP_SHEET_ID,
    match_sku_tokens,
    parse_brand_slugs,
)
from events.event_confirmations import (
    EventConfirmationMailer,
    build_subject,
)
from events.models import EventConfirmation, Product, ProductType, TimeZone
from events.sheet_event_confirmations import (
    STATUS_SENT,
    SheetRowPayload,
    _parse_yearless_sheet_date,
    payload_from_mapping,
    send_from_sheet_row,
    validate_sheet_id,
)
from tenants.models import Tenant

EASTERN = ZoneInfo("America/New_York")

FAMUS_CATALOG = [
    "Havana Nights (Mint & Lime) 16oz",
    "OG Luxe (Tropical Ice) 16oz",
]
WOOK_CATALOG = [
    "Berry Groovy 12oz Can",
    "Laser Lime 12oz Can",
    "Pineapple Peak 12oz Can",
]
ARMOR_CATALOG = [
    "Boujee Berry 2oz Shot",
    "Slayberry 2oz Shot",
]
FAMUS_RESOURCES = [
    {
        "label": "Brand Guidelines",
        "kind": "pdf",
        "url": "/training/famus-energy/brand-guidelines.pdf",
    },
    {
        "label": "BA FAQ Guide",
        "kind": "pdf",
        "url": "/training/famus-energy/ba-faq-guide.pdf",
    },
]
TORCH_CHROME = (
    "Hey ",
    "letter-spacing: 1.2px",
    "background-color: #f7f7f8",
    "Clock in / Clock out",
    "Open Recap Form",
    "The Ignite Team",
    "Ignite Productions LLC",
    "This is an automated message.",
)


def test_column_h_routes_one_brand_and_refuses_unknown():
    assert parse_brand_slugs("Famus Energy") == ["famus-energy"]
    assert parse_brand_slugs("Alcohol Armour") == ["alcohol-armor"]
    assert parse_brand_slugs("WookWater") == ["wook-water"]
    assert parse_brand_slugs("Famus Energy / Wook Water") == [
        "famus-energy",
        "wook-water",
    ]
    assert parse_brand_slugs("Famus Energy and Alcohol Armor") == [
        "famus-energy",
        "alcohol-armor",
    ]
    with pytest.raises(ValueError, match="Brand is required"):
        parse_brand_slugs("")
    with pytest.raises(ValueError, match="Unknown brand"):
        parse_brand_slugs("Torch")


def test_skus_stay_on_the_named_catalog():
    famus = {"famus-energy": FAMUS_CATALOG}
    assert match_sku_tokens(["OG Luxe", "Havana Nights"], famus) == [
        "OG Luxe (Tropical Ice) 16oz",
        "Havana Nights (Mint & Lime) 16oz",
    ]
    wook = {"wook-water": WOOK_CATALOG}
    assert match_sku_tokens(
        ["Berry Groovy 12oz Can", "Laser Lime 12oz Can"], wook
    ) == WOOK_CATALOG[:2]
    with pytest.raises(ValueError, match="not in the catalog"):
        match_sku_tokens(["Berry Groovy 12oz Can"], famus)
    with pytest.raises(ValueError, match="not in the catalog"):
        match_sku_tokens(["Berry Groovy 4-Pack"], wook)
    with pytest.raises(ValueError, match="not in the catalog"):
        match_sku_tokens(["Boujee Berry 12-Pack"], {"alcohol-armor": ARMOR_CATALOG})
    both = {"famus-energy": FAMUS_CATALOG, "wook-water": WOOK_CATALOG}
    resolved = match_sku_tokens(["OG Luxe", "Pineapple Peak 12oz Can"], both)
    assert resolved == [
        "OG Luxe (Tropical Ice) 16oz",
        "Pineapple Peak 12oz Can",
    ]
    with pytest.raises(ValueError, match="not in the catalog"):
        match_sku_tokens(["Slayberry 2oz Shot"], both)


def test_yearless_sheet_date_uses_the_upcoming_year():
    assert _parse_yearless_sheet_date("Oct 21", today=date(2026, 10, 19)) == date(
        2026, 10, 21
    )
    assert _parse_yearless_sheet_date("Jan 5", today=date(2026, 12, 20)) == date(
        2027, 1, 5
    )


def test_payload_reads_brand_column_and_confirmed_ba_name():
    payload = payload_from_mapping(
        {
            "Date": "Oct 21, 2026",
            "Start Time": "7a",
            "End Time": "10a",
            "Store Name": "marathon",
            "Brand": "Famus Energy",
            "SKUs": "OG Luxe, Havana Nights",
            "Confirmed BA Name": "Alex BA",
            "Email": "alex@example.com",
        },
        row_number=2,
        sheet_id=BRAND_GROUP_SHEET_ID,
    )
    assert payload.brand == "Famus Energy"
    assert payload.ba_name == "Alex BA"
    assert payload.skus == "OG Luxe, Havana Nights"
    assert validate_sheet_id(BRAND_GROUP_SHEET_ID) == BRAND_GROUP_SHEET_ID


def test_sent_row_is_not_emailed_again():
    payload = SheetRowPayload(
        row_number=4,
        sheet_id=BRAND_GROUP_SHEET_ID,
        confirmation_status=STATUS_SENT,
        ba_name="Alex",
        ba_email="alex@example.com",
        date="Oct 21, 2026",
        start_time="7a",
        brand="Famus Energy",
    )
    result = send_from_sheet_row(payload)
    assert result.ok is False
    assert result.details.get("already_sent") is True
    assert "Already Sent" in result.message


def _system_user():
    from django.contrib.auth import get_user_model
    from tenants.tests.base import ensure_role

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


def _brand(slug, name, code, resources=None):
    user = _system_user()
    tenant = Tenant.objects.filter(slug=slug).order_by("id").first()
    if tenant is None:
        holder = Tenant.objects.filter(checkin_code=code).first()
        if holder is not None and holder.slug != slug:
            holder.checkin_code = None
            holder.save(update_fields=["checkin_code"])
        tenant = Tenant.objects.create(
            name=name,
            slug=slug,
            request_url_name=slug,
            checkin_code=code,
            checkin_resources=resources or [],
            created_by=user,
        )
    else:
        holder = (
            Tenant.objects.filter(checkin_code=code).exclude(pk=tenant.pk).first()
        )
        if holder is not None:
            holder.checkin_code = None
            holder.save(update_fields=["checkin_code"])
        tenant.name = name
        tenant.checkin_code = code
        tenant.checkin_resources = resources or []
        tenant.save(
            update_fields=["name", "checkin_code", "checkin_resources"]
        )
    product_type, _ = ProductType.objects.get_or_create(
        tenant=tenant,
        name="Catalog",
        defaults={"created_by": user},
    )
    return tenant, product_type, user


def _products(tenant, product_type, user, names):
    for name in names:
        Product.objects.get_or_create(
            tenant=tenant,
            name=name,
            defaults={
                "product_type": product_type,
                "created_by": user,
            },
        )


def _confirmation(tenant, **kwargs):
    tz_row, _ = TimeZone.objects.get_or_create(
        name="America/New_York", defaults={"code": "ET", "offset": -300}
    )
    defaults = dict(
        tenant=tenant,
        timezone=tz_row,
        timezone_name="America/New_York",
        ba_name="Alex BA",
        ba_email="alex@example.com",
        store_name="marathon",
        address="1315 Elkhorn Rd, Campbellsville, KY 42718, USA",
        event_type_label="Retail Sampling",
        starts_at=datetime(2026, 10, 21, 7, 0, tzinfo=EASTERN),
        ends_at=datetime(2026, 10, 21, 10, 0, tzinfo=EASTERN),
        products=["OG Luxe (Tropical Ice) 16oz"],
        send_reminders=True,
    )
    defaults.update(kwargs)
    return EventConfirmation.objects.create(**defaults)


def _html(confirmation):
    mailer = EventConfirmationMailer(confirmation, EventConfirmation.STAGE_BOOKED)
    envelope = mailer.envelope()
    assert envelope.template == "events.templates.emails.event_confirmation"
    return envelope.render_template(), envelope.subject


@pytest.mark.django_db
class TestBrandGroupSheetSend:
    def setup_method(self):
        self.famus, famus_type, user = _brand(
            "famus-energy",
            "FAMUS Energy",
            "FM-PAG4TX",
            FAMUS_RESOURCES,
        )
        self.wook, wook_type, user = _brand(
            "wook-water", "Wook Water", "WW-36DJAJ"
        )
        self.armor, armor_type, user = _brand(
            "alcohol-armor", "Alcohol Armor", "AA-RPP85E"
        )
        _products(self.famus, famus_type, user, FAMUS_CATALOG)
        _products(self.wook, wook_type, user, WOOK_CATALOG)
        _products(self.armor, armor_type, user, ARMOR_CATALOG)

    def _send(self, **values):
        body = {
            "Date": "Oct 21, 2026",
            "Start Time": "7a",
            "End Time": "10a",
            "Store Name": "marathon",
            "Address": "1315 Elkhorn Rd, Campbellsville, KY 42718, USA",
            "State": "KY",
            "Confirmed BA Name": "Alex BA",
            "Email": "alex@example.com",
        }
        body.update(values)
        payload = payload_from_mapping(
            body,
            row_number=2,
            sheet_id=BRAND_GROUP_SHEET_ID,
            dry_run=True,
        )
        with patch(
            "events.sheet_event_confirmations.resolve_timezone_for_row",
            return_value=("America/New_York", ""),
        ):
            return send_from_sheet_row(payload)

    def test_famus_only_link_and_catalog(self):
        result = self._send(Brand="Famus Energy", SKUs="OG Luxe, Havana Nights")
        assert result.ok is True
        assert result.details["brand_slugs"] == ["famus-energy"]
        assert result.details["checkin_urls"] == [
            "https://client.igniteproductions.co/checkin/FM-PAG4TX"
        ]
        assert result.details["products"] == [
            "OG Luxe (Tropical Ice) 16oz",
            "Havana Nights (Mint & Lime) 16oz",
        ]
        assert "WW-36DJAJ" not in "".join(result.details["checkin_urls"])
        assert "AA-RPP85E" not in "".join(result.details["checkin_urls"])

    def test_wook_only_link(self):
        result = self._send(
            Brand="Wook Water",
            SKUs="Berry Groovy 12oz Can, Laser Lime 12oz Can, Pineapple Peak 12oz Can",
        )
        assert result.ok is True
        assert result.details["brand_slugs"] == ["wook-water"]
        assert result.details["checkin_urls"] == [
            "https://client.igniteproductions.co/checkin/WW-36DJAJ"
        ]
        assert "FM-PAG4TX" not in "".join(result.details["checkin_urls"])

    def test_alcohol_armour_spelling_uses_armor_link(self):
        result = self._send(
            Brand="Alcohol Armour",
            SKUs="Boujee Berry 2oz Shot, Slayberry 2oz Shot",
        )
        assert result.ok is True
        assert result.details["brand_slugs"] == ["alcohol-armor"]
        assert result.details["checkin_urls"] == [
            "https://client.igniteproductions.co/checkin/AA-RPP85E"
        ]

    def test_two_brands_omit_the_third(self):
        result = self._send(
            Brand="Famus Energy, Wook Water",
            SKUs="OG Luxe, Pineapple Peak 12oz Can",
        )
        assert result.ok is True
        assert result.details["brand_slugs"] == ["famus-energy", "wook-water"]
        assert result.details["checkin_urls"] == [
            "https://client.igniteproductions.co/checkin/FM-PAG4TX",
            "https://client.igniteproductions.co/checkin/WW-36DJAJ",
        ]
        assert result.details["products"] == [
            "OG Luxe (Tropical Ice) 16oz",
            "Pineapple Peak 12oz Can",
        ]
        assert "AA-RPP85E" not in "".join(result.details["checkin_urls"])

    def test_wook_sku_on_a_famus_row_is_refused(self):
        result = self._send(Brand="Famus Energy", SKUs="Berry Groovy 12oz Can")
        assert result.ok is False
        assert "not in the catalog" in result.message

    def test_blank_brand_does_not_attach_every_brand(self):
        result = self._send(Brand="", SKUs="OG Luxe")
        assert result.ok is False
        assert "Brand is required" in result.message
        assert EventConfirmation.objects.count() == 0


@pytest.mark.django_db
class TestBrandGroupEmailUsesTorchTemplate:
    def setup_method(self):
        self.famus, _, _ = _brand(
            "famus-energy", "FAMUS Energy", "FM-PAG4TX", FAMUS_RESOURCES
        )
        self.wook, _, _ = _brand("wook-water", "Wook Water", "WW-36DJAJ")
        self.armor, _, _ = _brand(
            "alcohol-armor", "Alcohol Armor", "AA-RPP85E"
        )

    def test_each_single_brand_and_a_pair_share_the_torch_chrome(self):
        cases = [
            (
                self.famus,
                ["famus-energy"],
                "Your FAMUS Energy Retail Sampling – marathon | 10/21/2026 | 7a - 10a",
                ["FM-PAG4TX", "brand-guidelines.pdf", "ba-faq-guide.pdf"],
                ["WW-36DJAJ", "AA-RPP85E"],
            ),
            (
                self.wook,
                ["wook-water"],
                "Your Wook Water Retail Sampling – marathon | 10/21/2026 | 7a - 10a",
                ["WW-36DJAJ"],
                ["FM-PAG4TX", "AA-RPP85E", "brand-guidelines.pdf", "ba-faq-guide.pdf"],
            ),
            (
                self.armor,
                ["alcohol-armor"],
                "Your Alcohol Armor Retail Sampling – marathon | 10/21/2026 | 7a - 10a",
                ["AA-RPP85E"],
                ["FM-PAG4TX", "WW-36DJAJ", "brand-guidelines.pdf", "ba-faq-guide.pdf"],
            ),
        ]
        for tenant, slugs, subject, present, absent in cases:
            confirmation = _confirmation(tenant, brand_slugs=slugs)
            html, rendered_subject = _html(confirmation)
            assert rendered_subject == subject
            assert build_subject(confirmation) == subject
            for marker in TORCH_CHROME:
                assert marker in html
            for bit in present:
                assert bit in html
            for bit in absent:
                assert bit not in html
            assert "spark.igniteproductions.co" not in html

        paired = _confirmation(
            self.famus,
            brand_slugs=["famus-energy", "wook-water"],
            products=[
                "OG Luxe (Tropical Ice) 16oz",
                "Pineapple Peak 12oz Can",
            ],
        )
        html, subject = _html(paired)
        assert subject == (
            "Your FAMUS Energy and Wook Water Retail Sampling – "
            "marathon | 10/21/2026 | 7a - 10a"
        )
        for marker in TORCH_CHROME:
            assert marker in html
        assert html.count("Clock in / Clock out") == 2
        assert html.count("Open Recap Form") == 2
        assert "FM-PAG4TX" in html
        assert "WW-36DJAJ" in html
        assert "brand-guidelines.pdf" in html
        assert "AA-RPP85E" not in html
        assert "Alcohol Armor" not in html
        assert "OG Luxe (Tropical Ice) 16oz" in html
        assert "Pineapple Peak 12oz Can" in html
        assert "Slayberry" not in html
        assert "spark.igniteproductions.co" not in html
