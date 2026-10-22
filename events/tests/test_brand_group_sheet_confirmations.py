"""Famus / Alcohol Armor / Wook Water sheet confirmations.

Column H picks the brands. The email is the Torch confirmation template.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from events.brand_group_sheet import (
    BRAND_GROUP_SHEET_ID,
    PIVOT_CHECKIN_CODE,
    PIVOT_SHEET_ID,
    match_sku_tokens,
    parse_brand_slugs,
)
from events.event_confirmations import (
    EventConfirmationMailer,
    SendResult,
    build_subject,
    due_reminders,
)
from events.models import EventConfirmation, Product, ProductType, TimeZone
from events.sheet_event_confirmation_views import BrandGroupSheetEventConfirmationView
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
    with pytest.raises(ValueError, match="Unknown brand"):
        parse_brand_slugs("Pivot Energy Drink")


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


def test_brand_group_status_tab_follows_gid():
    """A renamed worksheet must not leave stamps aimed at Sheet1."""
    from events.sheet_event_confirmations import BRAND_GROUP_CONFIG

    assert BRAND_GROUP_CONFIG.tab_title is None
    assert BRAND_GROUP_CONFIG.tab_gid == 0
    assert BRAND_GROUP_CONFIG.known_status_cols["Confirmation Status"] == "W"


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
        assert "PE-Y7VPYC" not in "".join(result.details["checkin_urls"])

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
                ["WW-36DJAJ", "AA-RPP85E", "retail-sampling-faq.pdf", "product-comparison.pdf"],
            ),
            (
                self.wook,
                ["wook-water"],
                "Your Wook Water Retail Sampling – marathon | 10/21/2026 | 7a - 10a",
                ["WW-36DJAJ"],
                [
                    "FM-PAG4TX",
                    "AA-RPP85E",
                    "brand-guidelines.pdf",
                    "ba-faq-guide.pdf",
                    "retail-sampling-faq.pdf",
                    "product-comparison.pdf",
                ],
            ),
            (
                self.armor,
                ["alcohol-armor"],
                "Your Alcohol Armor Retail Sampling – marathon | 10/21/2026 | 7a - 10a",
                ["AA-RPP85E"],
                [
                    "FM-PAG4TX",
                    "WW-36DJAJ",
                    "brand-guidelines.pdf",
                    "ba-faq-guide.pdf",
                    "retail-sampling-faq.pdf",
                    "product-comparison.pdf",
                ],
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
        assert "retail-sampling-faq.pdf" not in html
        assert "product-comparison.pdf" not in html
        assert "spark.igniteproductions.co" not in html


PIVOT_CATALOG = [
    "Blue Razz",
    "Pink Lemonade",
    "Melon Rush",
    "Going Grape",
]
PIVOT_RESOURCES = [
    {
        "label": "BA Guide",
        "kind": "pdf",
        "url": "/training/pivot-energy-drink/ba-guide.pdf",
    },
]
PIVOT_FAQ_URL = (
    "https://client.igniteproductions.co/training/pivot-energy-drink/"
    "retail-sampling-faq.pdf"
)
PIVOT_COMPARISON_URL = (
    "https://client.igniteproductions.co/training/pivot-energy-drink/"
    "product-comparison.pdf"
)


def test_pivot_sheet_id_is_allowlisted_and_famus_stays_the_default():
    assert validate_sheet_id(PIVOT_SHEET_ID) == PIVOT_SHEET_ID
    assert validate_sheet_id(BRAND_GROUP_SHEET_ID) == BRAND_GROUP_SHEET_ID
    assert (
        BrandGroupSheetEventConfirmationView.default_sheet_id == BRAND_GROUP_SHEET_ID
    )
    with pytest.raises(ValueError, match="unknown sheet"):
        validate_sheet_id("not-a-sheet")


@pytest.mark.django_db
class TestPivotSheetSend:
    def setup_method(self):
        self.pivot, product_type, user = _brand(
            "pivot-energy-drink",
            "Pivot Energy Drink",
            PIVOT_CHECKIN_CODE,
            PIVOT_RESOURCES,
        )
        _products(self.pivot, product_type, user, PIVOT_CATALOG)
        self.famus, famus_type, user = _brand(
            "famus-energy",
            "FAMUS Energy",
            "FM-PAG4TX",
            FAMUS_RESOURCES,
        )
        _products(self.famus, famus_type, user, FAMUS_CATALOG)

    def _row(self, **values):
        body = {
            "Date": "Oct 30, 2026",
            "Start Time": "2p",
            "End Time": "5p",
            "Store Name": "Total Wine (Fort Myers)",
            "Address": "13711 S Tamiami Trail, Fort Myers, FL 33912",
            "State": "FL",
            "SKUs to sample": "Blue Razz\nPink Lemonade\nMelon Rush\nGoing Grape",
            "BA Name": "Pat BA",
            "Email": "pivot-ba@example.com",
        }
        body.update(values)
        return body

    def _send(self, **values):
        payload = payload_from_mapping(
            self._row(**values),
            row_number=2,
            sheet_id=PIVOT_SHEET_ID,
            dry_run=True,
        )
        with patch(
            "events.sheet_event_confirmations.resolve_timezone_for_row",
            return_value=("America/New_York", ""),
        ):
            return send_from_sheet_row(payload)

    def test_every_row_is_pivot_even_if_a_brand_cell_is_present(self):
        result = self._send(Brand="Famus Energy")
        assert result.ok is True
        assert result.details["brand_slugs"] == []
        assert result.details["checkin_urls"] == [
            "https://client.igniteproductions.co/checkin/PE-Y7VPYC"
        ]
        assert result.details["products"] == PIVOT_CATALOG
        assert result.details["send_reminders"] is True
        joined = " ".join(result.details["checkin_urls"])
        assert "FM-PAG4TX" not in joined
        assert "WW-36DJAJ" not in joined
        assert "AA-RPP85E" not in joined

    def test_famus_sku_on_the_pivot_sheet_is_refused(self):
        result = self._send(**{"SKUs to sample": "OG Luxe"})
        assert result.ok is False
        assert "not in the catalog" in result.message
        assert "pivot-energy-drink" in result.message

    def test_blank_skus_send_with_no_product_lines(self):
        result = self._send(**{"SKUs to sample": ""})
        assert result.ok is True
        assert result.details["products"] == []

    def test_sent_row_is_not_emailed_again(self):
        payload = SheetRowPayload(
            row_number=2,
            sheet_id=PIVOT_SHEET_ID,
            confirmation_status=STATUS_SENT,
            ba_name="Pat BA",
            ba_email="pivot-ba@example.com",
            date="Oct 30, 2026",
            start_time="2p",
        )
        result = send_from_sheet_row(payload)
        assert result.ok is False
        assert result.details.get("already_sent") is True
        assert EventConfirmation.objects.count() == 0

    def test_standing_code_is_not_reminted(self):
        self.pivot.checkin_code = "PE-OTHER1"
        self.pivot.save(update_fields=["checkin_code"])
        result = self._send()
        assert result.ok is False
        assert PIVOT_CHECKIN_CODE in result.message
        self.pivot.refresh_from_db()
        assert self.pivot.checkin_code == "PE-OTHER1"

    def test_reminders_are_scheduled_on_the_torch_sweep(self):
        from django.utils import timezone as dj_tz

        when = dj_tz.now().astimezone(EASTERN) + timedelta(days=10)
        date_label = f"{when.strftime('%b')} {when.day}, {when.year}"
        payload = payload_from_mapping(
            self._row(Date=date_label),
            row_number=2,
            sheet_id=PIVOT_SHEET_ID,
            dry_run=False,
        )
        with (
            patch(
                "events.sheet_event_confirmations.resolve_timezone_for_row",
                return_value=("America/New_York", ""),
            ),
            patch("events.sheet_event_confirmations.write_row_status"),
            patch(
                "events.event_confirmations.send_confirmation_stage",
                return_value=SendResult(stage="booked", sent=True),
            ),
        ):
            result = send_from_sheet_row(payload)
        assert result.ok is True
        confirmation = EventConfirmation.objects.get(ba_email="pivot-ba@example.com")
        assert confirmation.send_reminders is True
        assert confirmation.tenant.slug == "pivot-energy-drink"
        assert confirmation.tenant.checkin_code == PIVOT_CHECKIN_CODE
        assert confirmation.event_type_label == "Retail Sampling"
        starts = confirmation.starts_at
        t24 = due_reminders(now=starts - timedelta(hours=24))
        t3 = due_reminders(now=starts - timedelta(hours=3))
        assert any(
            row.id == confirmation.id and stage == EventConfirmation.STAGE_T24
            for row, stage in t24
        )
        assert any(
            row.id == confirmation.id and stage == EventConfirmation.STAGE_T3
            for row, stage in t3
        )
        t3_html = (
            EventConfirmationMailer(confirmation, EventConfirmation.STAGE_T3)
            .envelope()
            .render_template()
        )
        assert (
            "Just a friendly reminder that you're scheduled for a" in t3_html
        )
        assert "Pivot Energy Drink retail sampling" in t3_html
        assert "in a few hours" in t3_html


@pytest.mark.django_db
class TestPivotEmailUsesTorchTemplate:
    def setup_method(self):
        self.pivot, _, _ = _brand(
            "pivot-energy-drink",
            "Pivot Energy Drink",
            PIVOT_CHECKIN_CODE,
            PIVOT_RESOURCES,
        )

    def test_faq_clock_and_recap_stay_on_the_pivot_checkin(self):
        confirmation = _confirmation(
            self.pivot,
            products=PIVOT_CATALOG,
            brand_slugs=[],
            store_name="Total Wine (Fort Myers)",
        )
        html, subject = _html(confirmation)
        assert subject == (
            "Your Pivot Energy Drink Retail Sampling – "
            "Total Wine (Fort Myers) | 10/21/2026 | 7a - 10a"
        )
        for marker in TORCH_CHROME:
            assert marker in html
        assert html.count("Clock in / Clock out") == 1
        assert html.count("Open Recap Form") == 1
        assert "PE-Y7VPYC" in html
        assert "BA Guide" in html
        assert "Retail Sampling FAQ" in html
        assert "Product Comparison" in html
        assert html.count("Retail Sampling FAQ") == 1
        assert html.count("Product Comparison") == 1
        assert html.count("retail-sampling-faq.pdf") == 3
        assert html.count("product-comparison.pdf") == 3
        assert PIVOT_FAQ_URL in html
        assert PIVOT_COMPARISON_URL in html
        assert "client.igniteproductions.co" in html
        for absent in (
            "FM-PAG4TX",
            "WW-36DJAJ",
            "AA-RPP85E",
            "spark.igniteproductions.co",
            "ba-faq-guide.pdf",
            "brand-guidelines.pdf",
        ):
            assert absent not in html

    def test_walkup_buttons_are_not_duplicated(self):
        self.pivot.checkin_resources = [
            *PIVOT_RESOURCES,
            {
                "label": "Retail Sampling FAQ",
                "kind": "pdf",
                "url": PIVOT_FAQ_URL,
            },
            {
                "label": "Product Comparison",
                "kind": "pdf",
                "url": PIVOT_COMPARISON_URL,
            },
        ]
        self.pivot.save(update_fields=["checkin_resources"])
        confirmation = _confirmation(self.pivot, products=["Blue Razz"])
        html, _subject = _html(confirmation)
        assert html.count("BA Guide") == 1
        assert html.count("Retail Sampling FAQ") == 1
        assert html.count("Product Comparison") == 1
        assert html.count("retail-sampling-faq.pdf") == 3
        assert html.count("product-comparison.pdf") == 3


# Retail Schedule grid on 2026-10-22. AX is column 50 and is not on it.
PIVOT_GRID_COLUMNS = 41
PIVOT_HEADERS = [
    "State",
    "Day of Week",
    "Date",
    "Store Number",
    "Store Name",
    "Start Time",
    "End Time",
    "Address",
    "Requested?",
    "Pre-Billed Demo Cost?",
    "Product Spend",
    "Billed Product Cost?",
    "One-Off Kit Cost",
    "Notes",
    "SKUs to sample",
    "BA Name",
    "BA Address",
    "Rate",
    "Email",
    "Cell Phone #",
    "Shipped?",
    "Send Confirmation",
    "Cancel Confirmation",
    "Force Resend",
    "Confirmation Status",
    "Confirmation Sent At",
    "Confirmation Error",
    "Spark Confirmation UUID",
    "Resend Confirmation",
]


class _SheetResp:
    status = 400
    reason = "Bad Request"


class _SheetReq:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _PivotSheets:
    """Minimal Sheets client. ``column_count`` is the live grid width."""

    def __init__(self, *, headers=None, column_count=PIVOT_GRID_COLUMNS, reject_past=False):
        self.headers = list(PIVOT_HEADERS if headers is None else headers)
        self.column_count = column_count
        self.reject_past = reject_past
        self._rejected = False
        self.batches: list[list] = []
        self.header_writes: list[dict] = []
        self.expands: list[int] = []
        self.meta_gets = 0
        self.header_gets: list[str] = []
        self.log: list[tuple] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        if "range" in kwargs:
            self.header_gets.append(kwargs["range"])

            def _header():
                return {"values": [list(self.headers)]}

            return _SheetReq(_header)

        self.meta_gets += 1

        def _meta():
            return {
                "sheets": [
                    {
                        "properties": {
                            "title": "Retail Schedule",
                            "sheetId": 0,
                            "gridProperties": {
                                "rowCount": 3569,
                                "columnCount": self.column_count,
                            },
                        }
                    }
                ]
            }

        return _SheetReq(_meta)

    def update(self, **kwargs):
        def _write():
            self.log.append(("header", kwargs.get("range")))
            self.header_writes.append(kwargs)
            values = (kwargs.get("body") or {}).get("values") or [[]]
            self.headers.extend(values[0])
            return {}

        return _SheetReq(_write)

    def batchUpdate(self, **kwargs):
        body = kwargs.get("body") or {}
        if "requests" in body:

            def _expand():
                for req in body["requests"]:
                    props = (req.get("updateSheetProperties") or {}).get("properties") or {}
                    cols = (props.get("gridProperties") or {}).get("columnCount")
                    if cols:
                        self.log.append(("expand", cols))
                        self.expands.append(cols)
                        self.column_count = cols
                return {}

            return _SheetReq(_expand)

        data = list(body.get("data") or [])

        def _values():
            self.batches.append(data)
            if (
                self.reject_past
                and not self._rejected
                and _ranges_past(data, self.column_count)
            ):
                self._rejected = True
                raise _grid_http_error(data[0]["range"], self.column_count)
            return {}

        return _SheetReq(_values)


def _ranges_past(data: list, limit: int) -> bool:
    from events.sheet_event_confirmations import _col_index

    for item in data:
        cell = str(item.get("range") or "").split("!")[-1]
        letters = ""
        for ch in cell:
            if ch.isalpha():
                letters += ch
            else:
                break
        if letters and _col_index(letters) > limit:
            return True
    return False


def _grid_http_error(a1: str, max_columns: int):
    import json

    from googleapiclient.errors import HttpError

    message = (
        "Invalid data[0]: Range ('"
        + a1
        + "') exceeds grid limits. Max rows: 3569, max columns: "
        + str(max_columns)
        + "."
    )
    content = ('{"error":{"code":400,"message":' + json.dumps(message) + "}}").encode()
    return HttpError(_SheetResp(), content)


def _assert_ranges_inside(ranges: list[str], limit: int = PIVOT_GRID_COLUMNS) -> None:
    from events.sheet_event_confirmations import _col_index

    assert ranges, "expected a status write"
    for a1 in ranges:
        cell = a1.split("!")[-1]
        letters = ""
        for ch in cell:
            if ch.isalpha():
                letters += ch
            else:
                break
        assert _col_index(letters) <= limit, a1


def test_pivot_status_columns_fit_the_retail_grid():
    """AX is column 50. The live Retail Schedule grid stops at AO (41)."""
    from events.sheet_event_confirmations import (
        BRAND_GROUP_CONFIG,
        PIVOT_CONFIG,
        SENT_STATUS_HEADER,
        _col_index,
    )

    cols = PIVOT_CONFIG.known_status_cols
    assert cols["Confirmation Status"] == "Y"
    assert cols["Confirmation Sent At"] == "Z"
    assert cols["Confirmation Error"] == "AA"
    assert cols["Spark Confirmation UUID"] == "AB"
    assert SENT_STATUS_HEADER not in cols
    assert "P" not in cols.values()
    assert "AX" not in cols.values()
    for letter in cols.values():
        assert _col_index(letter) <= PIVOT_GRID_COLUMNS
    assert BRAND_GROUP_CONFIG.tab_title is None
    assert BRAND_GROUP_CONFIG.known_status_cols["Confirmation Status"] == "W"


def test_write_row_status_stays_inside_the_pivot_grid():
    from events.sheet_event_confirmations import (
        PIVOT_SHEET_ID,
        STATUS_SENT,
        write_row_status,
    )

    sheets = _PivotSheets()
    with patch(
        "events.sheet_event_confirmations._service", return_value=sheets
    ):
        write_row_status(
            sheet_id=PIVOT_SHEET_ID,
            row_number=2,
            status=STATUS_SENT,
            error="",
            confirmation_uuid="11111111-1111-1111-1111-111111111111",
            sent_at="2026-10-22 11:30 EDT",
            sent_column_value="Sent 2026-10-22 11:30 EDT",
        )
    assert sheets.meta_gets == 0
    assert sheets.header_gets == []
    ranges = [item["range"] for item in sheets.batches[-1]]
    _assert_ranges_inside(ranges)
    assert "'Retail Schedule'!Y2" in ranges
    assert "'Retail Schedule'!Z2" in ranges
    assert "'Retail Schedule'!AA2" in ranges
    assert "'Retail Schedule'!AB2" in ranges
    assert not any("!P2" in a1 or "!AX" in a1 for a1 in ranges)


def test_column_overflow_stamps_the_real_header():
    """A stale AX letter must be rewritten onto column Y, not retried."""
    from events.sheet_event_confirmations import (
        PIVOT_CONFIG,
        PIVOT_SHEET_ID,
        STATUS_SENT,
        write_row_status,
    )

    saved = dict(PIVOT_CONFIG.known_status_cols)
    sheets = _PivotSheets(reject_past=True)
    try:
        PIVOT_CONFIG.known_status_cols.clear()
        PIVOT_CONFIG.known_status_cols.update(
            {
                "Event Confirmation Sent?": "P",
                "Confirmation Status": "AX",
                "Confirmation Sent At": "AY",
                "Confirmation Error": "AZ",
                "Spark Confirmation UUID": "BA",
            }
        )
        with patch(
            "events.sheet_event_confirmations._service", return_value=sheets
        ):
            write_row_status(
                sheet_id=PIVOT_SHEET_ID,
                row_number=2,
                status=STATUS_SENT,
                error="",
                confirmation_uuid="22222222-2222-2222-2222-222222222222",
                sent_at="2026-10-22 11:30 EDT",
                sent_column_value="Sent 2026-10-22 11:30 EDT",
            )
    finally:
        PIVOT_CONFIG.known_status_cols.clear()
        PIVOT_CONFIG.known_status_cols.update(saved)

    assert len(sheets.batches) == 2
    recovered = [item["range"] for item in sheets.batches[-1]]
    _assert_ranges_inside(recovered)
    assert "'Retail Schedule'!Y2" in recovered
    assert not any("!AX" in a1 or "!P2" in a1 for a1 in recovered)
    assert sheets.header_gets == ["'Retail Schedule'!A1:AO1"]
    assert sheets.expands == []
    values = {item["range"]: item["values"][0][0] for item in sheets.batches[-1]}
    assert values["'Retail Schedule'!Y2"] == STATUS_SENT


def test_missing_status_header_expands_the_grid_before_append():
    from events.sheet_event_confirmations import (
        PIVOT_CONFIG,
        PIVOT_SHEET_ID,
        _col_index,
        _resolve_stamp_columns,
    )

    sheets = _PivotSheets(headers=["Taken"] * PIVOT_GRID_COLUMNS)
    found = _resolve_stamp_columns(
        sheets,
        sheet_id=PIVOT_SHEET_ID,
        tab="Retail Schedule",
        config=PIVOT_CONFIG,
        wanted=["Confirmation Status"],
    )
    assert sheets.log[0] == ("expand", PIVOT_GRID_COLUMNS + 1)
    assert sheets.log[1][0] == "header"
    assert sheets.header_writes[0]["range"] == "'Retail Schedule'!AP1:AP1"
    assert sheets.header_writes[0]["body"]["values"] == [["Confirmation Status"]]
    assert found["Confirmation Status"] == "AP"
    assert _col_index(found["Confirmation Status"]) == sheets.column_count


def test_pivot_blank_status_stamps_sent_without_another_email():
    """Row 2 already mailed and the status cell is blank. Do not email again."""
    from events.sheet_event_confirmations import PIVOT_SHEET_ID, STATUS_SENT

    payload = SheetRowPayload(
        row_number=2,
        sheet_id=PIVOT_SHEET_ID,
        confirmation_status="",
        ba_name="Dalia E",
        ba_email="boxinglatina@gmail.com",
        store_name="Total Wine (Fort Myers)",
        date="Oct 23, 2026",
        start_time="2p",
        end_time="5p",
        address="13711 S Tamiami Trail, Fort Myers, FL 33912",
        state="FL",
        skus="Blue Razz",
    )
    mailed = type(
        "C",
        (),
        {
            "uuid": "33333333-3333-3333-3333-333333333333",
            "ba_email": "boxinglatina@gmail.com",
            "timezone_name": "America/New_York",
            "pk": 2,
            "store_name": "Total Wine (Fort Myers)",
        },
    )()
    sheets = _PivotSheets()
    with patch(
        "events.sheet_event_confirmations._find_mailed_confirmation",
        return_value=mailed,
    ) as find, patch(
        "events.sheet_event_confirmations._service", return_value=sheets
    ), patch(
        "events.sheet_event_confirmations._create_and_send",
        side_effect=AssertionError("must not email a shift that already went out"),
    ):
        result = send_from_sheet_row(payload)
    find.assert_called_once_with(payload, exact_only=True)
    assert result.ok is True
    assert result.status == STATUS_SENT
    assert result.details.get("already_sent") is True
    assert "no new email" in result.message.lower()
    ranges = [item["range"] for item in sheets.batches[-1]]
    _assert_ranges_inside(ranges)
    values = {item["range"]: item["values"][0][0] for item in sheets.batches[-1]}
    assert values["'Retail Schedule'!Y2"] == STATUS_SENT
    assert "'Retail Schedule'!P2" not in values


@pytest.mark.django_db
def test_pivot_row2_repair_silences_older_reminders_and_stamps_sent():
    """Duplicates of the already-mailed Fort Myers shift: newest keeps reminders."""
    from django.utils import timezone as dj_tz

    from events.brand_group_sheet import PIVOT_TENANT_SLUG
    from events.models import EventConfirmation, EventConfirmationSend
    from events.sheet_event_confirmations import (
        STATUS_SENT,
        repair_pivot_retail_row2,
    )

    tenant, _product_type, _user = _brand(
        PIVOT_TENANT_SLUG,
        "Pivot Energy Drink",
        PIVOT_CHECKIN_CODE,
    )
    starts = datetime(2026, 10, 23, 14, 0, tzinfo=EASTERN)
    older = _confirmation(
        tenant,
        ba_name="Dalia E",
        ba_email="boxinglatina@gmail.com",
        store_name="Total Wine (Fort Myers)",
        address="13711 S Tamiami Trail, Fort Myers, FL 33912",
        starts_at=starts,
        send_reminders=True,
    )
    newer = _confirmation(
        tenant,
        ba_name="Dalia E",
        ba_email="boxinglatina@gmail.com",
        store_name="Total Wine (Fort Myers)",
        address="13711 S Tamiami Trail, Fort Myers, FL 33912",
        starts_at=starts,
        send_reminders=True,
    )
    other = _confirmation(
        tenant,
        ba_name="Sofia Vallejo",
        ba_email="soso_vallejo@hotmail.com",
        store_name="Total Wine (Palm Beach Gardens)",
        starts_at=starts,
        send_reminders=True,
    )
    for conf in (older, newer):
        EventConfirmationSend.objects.create(
            confirmation=conf,
            stage=EventConfirmation.STAGE_BOOKED,
            to_email=conf.ba_email,
            sent_at=dj_tz.now(),
            attempts=1,
        )

    with patch(
        "events.sheet_event_confirmations.write_row_status"
    ) as write, patch(
        "events.event_confirmations.send_confirmation_stage"
    ) as mail:
        result = repair_pivot_retail_row2()

    mail.assert_not_called()
    older.refresh_from_db()
    newer.refresh_from_db()
    other.refresh_from_db()
    assert older.send_reminders is False
    assert newer.send_reminders is True
    assert other.send_reminders is True
    assert result["matches"] == 2
    assert result["mailed"] == 2
    assert result["reminders_updated"] == 1
    assert result["newest_uuid"] == str(newer.uuid)
    write.assert_called_once()
    stamped = write.call_args.kwargs
    assert stamped["sheet_id"] == PIVOT_SHEET_ID
    assert stamped["row_number"] == 2
    assert stamped["status"] == STATUS_SENT
    assert stamped["confirmation_uuid"] == str(newer.uuid)

