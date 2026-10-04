"""Feel Free Event Activation: sampling-only template on the FF-YMMK3Q walk-up."""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.payable_mileage import is_mileage_custom_field
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event
from recaps.management.commands.setup_feel_free_event_activation import (
    ACTIVATION_BUCKETS,
    COMPETITOR_FIELD,
    SAMPLE_QTY_LAYOUT,
    SPEC,
    TEMPLATE_NAME,
    build_spec,
)
from recaps.management.commands.setup_torch_event_activation import (
    EVENT_LABEL,
    SALES_FIELD_RE,
)
from recaps.models import (
    CustomField,
    CustomFieldValue,
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapTemplate,
    FileRecapCategory,
    RecapSection,
)
from recaps.tenant_overview import _ACTIVATION_BUCKETS, tenant_conversion_kpis
from recaps.types import _CONSUMERS_SAMPLED_RE, _PEOPLE_ENGAGED_RE
from tenants.models import Tenant

VALID_SECRET = "test-cron-secret-value-only-for-tests"
URL = "/internal/cron/setup-feel-free-event-activation"
MARKETS = ["Miami, FL", "Austin, TX", "San Antonio, TX"]

# Effect / health vocabulary that must never appear on a Feel Free form.
CLAIM_RE = re.compile(
    r"relax|energ|mood|wellness|benefit|helps?\b|calm|focus|euphori|buzz|"
    r"effect|stress|anxi|sleep|kratom|\bfeel(?!\s+free)|\bhigh\b",
    re.I,
)


def _labels(spec=SPEC) -> list[str]:
    return [name for _, fields in spec for name, *_ in fields]


def _copy(spec=SPEC) -> list[str]:
    out: list[str] = []
    for _, fields in spec:
        for name, _kind, _req, options, placeholder in fields:
            out += [name, placeholder, *options]
    out += [b.get("helper", "") for b in ACTIVATION_BUCKETS]
    out += [b["name"] for b in ACTIVATION_BUCKETS]
    out += list(SAMPLE_QTY_LAYOUT.values())
    return [s for s in out if s]


class TestSpec:
    def test_sampling_only_no_sales_fields(self):
        for label in _labels():
            assert not SALES_FIELD_RE.search(label), label
        blob = " ".join(_labels()).lower()
        for banned in ("units sold", "account spend", "conversion", "revenue"):
            assert banned not in blob

    def test_no_health_or_effect_claims(self):
        for text in _copy():
            # Booth traffic options are the only allowed "High".
            if text in ("High", "Medium", "Low"):
                continue
            assert not CLAIM_RE.search(text), text

    def test_feel_free_copy_only(self):
        blob = " ".join(_copy())
        for other in (
            "Torch",
            "Liquid Death",
            "Mark Anthony",
            "White Claw",
            "Neutonic",
            "Hiyo",
            "cans",
            "THC",
        ):
            assert other.lower() not in blob.lower(), other
        assert "Feel Free" in blob

    def test_sections_and_optional_questions(self):
        assert [s for s, _ in SPEC] == [
            "Event Details",
            "Consumer Engagement",
            "Feedback & Account Notes",
            "Products Sampled",
        ]
        optional = {f[0] for _, fields in SPEC for f in fields if not f[2]}
        assert optional == {
            COMPETITOR_FIELD,
            "Positive stories from the event",
            "Event organizer / venue feedback",
            "Anything you'd change or do differently?",
        }

    def test_mileage_field_is_auto_filled_from_itinerary(self):
        mileage = [n for n in _labels() if is_mileage_custom_field(n)]
        assert mileage == ["Mileage"]

    def test_products_sampled_from_catalog(self):
        opts = ["Tonic — Classic", "Tonic — Kava Mate"]
        section, fields = build_spec(opts)[-1]
        assert section == "Products Sampled"
        name, kind, required, options, _ = fields[0]
        assert (name, kind, required, options) == (
            "Products Sampled",
            "multiselect",
            True,
            opts,
        )
        assert SPEC[-1][1][0][3] == []
        assert SAMPLE_QTY_LAYOUT["sampleQtyLabel"] == "Samples distributed"

    def test_consumers_sampled_and_people_engaged_stay_distinct(self):
        sampled = [n for n in _labels() if _CONSUMERS_SAMPLED_RE.search(n)]
        assert sampled == ["How many TOTAL consumers did you sample?"]
        engaged = [n for n in _labels() if _PEOPLE_ENGAGED_RE.search(n)]
        assert engaged == ["People engaged"]

    def test_buckets_as_event_not_retail_or_onprem(self):
        for name in (EVENT_LABEL, TEMPLATE_NAME):
            hit = next(key for key, _, pat in _ACTIVATION_BUCKETS if pat.search(name))
            assert hit == "event"
            assert not re.search(r"retail|on[-\s]?prem|bar|venue", name, re.I)


@pytest.mark.django_db(transaction=True)
class TestSetupCommand(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.sys.is_superuser = True
        self.sys.save(update_fields=["is_superuser"])
        self.tenant = self.create_tenant(
            name="Feel Free", slug="feel-free", request_url_name="bl00-feel-free"
        )
        self.tenant.checkin_code = "FF-YMMK3Q"
        self.tenant.checkin_location_mode = Tenant.CHECKIN_LOCATION_MARKET
        self.tenant.checkin_markets = MARKETS
        self.tenant.save()
        # Prod shape: no pin, no picker, no buckets; walk-ups stamp the
        # lowest-id type and resolve the sole field-sampling form.
        self.default = self.create_event_type("Retail Sampling", self.tenant)
        self.field = self.create_event_type("Field Sampling", self.tenant)
        self.number = CustomRecapFieldType.objects.create(
            name="number", created_by=self.sys
        )
        self.retail_tpl = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Feel Free - Field Sampling",
            event_type=self.field,
            created_by=self.sys,
        )
        self.retail_section = RecapSection.objects.create(
            name="Consumer Engagement", order=1, tenant=self.tenant, created_by=self.sys
        )
        CustomField.objects.create(
            name="How many TOTAL consumers did you sample?",
            custom_recap_template=self.retail_tpl,
            custom_field_type=self.number,
            recap_section=self.retail_section,
            created_by=self.sys,
        )
        FileRecapCategory.objects.create(
            name="Receipts", tenant=self.tenant, created_by=self.sys
        )

    def _run(self, *args) -> str:
        out = io.StringIO()
        call_command("setup_feel_free_event_activation", *args, stdout=out)
        return out.getvalue()

    def _ea_template(self):
        return CustomRecapTemplate.objects.filter(
            tenant=self.tenant, name=TEMPLATE_NAME
        ).first()

    def _identify(self, **extra):
        body = {
            "firstName": "Pat",
            "lastName": "Sampler",
            "phone": "5550142",
            "eventDate": timezone.localdate().isoformat(),
        }
        body.update(extra)
        return Client().post(
            reverse("events.public_checkin_identify", kwargs={"code": "FF-YMMK3Q"}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_dry_run_writes_nothing(self):
        log = self._run()
        assert "DRY-RUN" in log
        assert "Walk-up default: 'Retail Sampling'" in log
        assert "Feel Free - Field Sampling" in log
        assert self._ea_template() is None
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_event_type_id is None
        assert not self.tenant.checkin_photo_buckets
        assert not self.tenant.checkin_event_types.exists()

    def test_apply_adds_program_and_keeps_retail_default(self):
        self._run("--apply")
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "FF-YMMK3Q"
        assert self.tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_MARKET
        assert self.tenant.checkin_event_type_id == self.default.id
        names = [et.name for et in self.tenant.checkin_event_types.order_by("id")]
        assert names == ["Retail Sampling", EVENT_LABEL]

        buckets = self.tenant.checkin_photo_buckets
        assert list(buckets) == [EVENT_LABEL]
        assert [b["name"] for b in buckets[EVENT_LABEL]] == [
            b["name"] for b in ACTIVATION_BUCKETS
        ]
        for b in ACTIVATION_BUCKETS:
            assert FileRecapCategory.objects.filter(
                tenant=self.tenant, name=b["name"]
            ).exists()

        tpl = self._ea_template()
        assert tpl.event_type.name == EVENT_LABEL
        assert tpl.product_samples is True and tpl.sales_performance is False
        assert tpl.layout == SAMPLE_QTY_LAYOUT
        fields = CustomField.objects.filter(custom_recap_template=tpl)
        assert sorted(f.name for f in fields) == sorted(_labels())
        assert not fields.filter(recap_section=self.retail_section).exists()
        assert (
            CustomField.objects.filter(custom_recap_template=self.retail_tpl).count()
            == 1
        )

    def test_apply_is_idempotent(self):
        self._run("--apply")
        log = self._run("--apply")
        assert "+0 ~0" in log
        assert "Pinned default : 'Retail Sampling' (unchanged)" in log
        assert CustomRecapTemplate.objects.filter(tenant=self.tenant).count() == 2

    def test_resolves_by_request_url_name(self):
        self.tenant.slug = "ff-prod"
        self.tenant.save(update_fields=["slug"])
        assert "Feel Free" in self._run()

    def test_retail_walkup_unchanged_and_activation_gets_its_form(self):
        self._run("--apply")
        self.tenant.refresh_from_db()
        ea = self._ea_template().event_type
        retail_event = self.create_event(
            name="Austin", tenant=self.tenant, event_type=self.default
        )
        ea_event = self.create_event(name="Fest", tenant=self.tenant, event_type=ea)

        assert (
            checkin_web.resolve_template_for_event(retail_event).id
            == self.retail_tpl.id
        )
        assert checkin_web.resolve_template_for_event(ea_event).name == TEMPLATE_NAME
        assert checkin_web.serialize_photo_buckets(retail_event) == []
        assert [b["name"] for b in checkin_web.serialize_photo_buckets(ea_event)] == [
            b["name"] for b in ACTIVATION_BUCKETS
        ]
        form = checkin_web.serialize_template(ea_event)
        assert form["sampleQtyLabel"] == "Samples distributed"
        assert checkin_web.serialize_template(retail_event)["sampleQtyLabel"] == ""

        ctx = checkin_web.build_tenant_context(self.tenant)
        assert ctx["locationMode"] == "market"
        assert [et["name"] for et in ctx["eventTypes"]] == [
            "Retail Sampling",
            EVENT_LABEL,
        ]

    def test_identify_market_for_retail_venue_for_activation(self):
        self._run("--apply")
        ea = self._ea_template().event_type

        retail = self._identify(eventTypeId=str(self.default.id), market="austin, tx")
        assert retail.status_code == 200, retail.content
        ev = Event.objects.get(uuid=retail.json()["event"]["uuid"])
        assert (ev.address, ev.event_type_id) == ("Austin, TX", self.default.id)

        venue = "2100 Barton Springs Rd, Austin, TX 78704"
        act = self._identify(
            eventTypeId=str(ea.id),
            address=venue,
            storeName="Zilker Park",
            phone="5550143",
        )
        assert act.status_code == 200, act.content
        ev = Event.objects.get(uuid=act.json()["event"]["uuid"])
        assert (ev.address, ev.event_type_id) == (venue, ea.id)

        # A cached page that still sends only the market keeps working.
        legacy = self._identify(
            eventTypeId=str(ea.id), market="Miami, FL", phone="5550144"
        )
        assert legacy.status_code == 200, legacy.content
        assert (
            Event.objects.get(uuid=legacy.json()["event"]["uuid"]).address
            == "Miami, FL"
        )

        # Retail still refuses a market that isn't on the list.
        bad = self._identify(
            eventTypeId=str(self.default.id), market="Nowhere", phone="5550145"
        )
        assert bad.status_code == 400

    def test_still_need_a_recap_tap_on_a_venue_shift(self):
        self._run("--apply")
        ea = self._ea_template().event_type
        venue = "2100 Barton Springs Rd, Austin, TX 78704"
        shift = self.create_event(name="Fest", tenant=self.tenant, event_type=ea)
        shift.address = venue
        shift.save(update_fields=["address"])
        with patch.object(checkin_web, "existing_shift_event_for", return_value=shift):
            res = self._identify(market=venue)
        assert res.status_code == 200, res.content
        assert res.json()["event"]["uuid"] == str(shift.uuid)

    def test_event_activation_recap_excluded_from_conversion_and_unapproved(self):
        self._run("--apply")
        tpl = self._ea_template()
        when = timezone.make_aware(
            datetime.combine(timezone.localdate(), datetime.min.time())
        )
        event = self.create_event(
            name="Fest", tenant=self.tenant, event_type=tpl.event_type, date=when
        )
        recap = CustomRecap.objects.create(
            name="Fest",
            approved=True,
            event=event,
            tenant=self.tenant,
            custom_recap_template=tpl,
            total_engagements=300,
            created_by=self.sys,
        )
        field = CustomField.objects.get(
            custom_recap_template=tpl, name="How many TOTAL consumers did you sample?"
        )
        CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=field, value="300", created_by=self.sys
        )
        today = timezone.localdate()
        kpis = tenant_conversion_kpis(self.tenant.id, today - timedelta(days=7), today)
        assert kpis["engagements"] == 0
        assert kpis["pct"] is None
        assert CustomRecap._meta.get_field("approved").default is False


@pytest.mark.django_db
class TestCronView:
    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_requires_secret(self, mock_call):
        resp = Client().post(URL, HTTP_X_CRON_SECRET="wrong")
        assert resp.status_code == 401
        mock_call.assert_not_called()

    @override_settings(INTERNAL_CRON_SECRET=VALID_SECRET)
    @patch("digest.cron_views.call_command")
    def test_dry_run_default_then_apply(self, mock_call):
        resp = Client().post(URL, HTTP_X_CRON_SECRET=VALID_SECRET)
        assert resp.status_code == 200
        args, kwargs = mock_call.call_args
        assert args[0] == "setup_feel_free_event_activation"
        assert "apply" not in kwargs
        resp = Client().post(URL, {"apply": "true"}, HTTP_X_CRON_SECRET=VALID_SECRET)
        assert mock_call.call_args.kwargs["apply"] is True
        assert resp.json()["applied"] is True
