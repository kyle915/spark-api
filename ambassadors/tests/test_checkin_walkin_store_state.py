"""Walk-in events carry a store and a state, so their recaps do too.

A standing-link check-in used to open its event with no retailer: the BA's
typed store only reached the title's parenthetical, and the state came only
from the address. The recap copies ``event.retailer`` / ``event.state`` at
submit, so every such recap landed under "No store recorded" / "No state
recorded" on the brand's dashboards.

Retail sampling check-ins now also have to name the store when they open a new
event. Activations (a venue, not a store) do not.
"""
import datetime
import json
import uuid

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone as dj_tz

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event, Retailer, RetailerAlias, State


@pytest.mark.django_db(transaction=True)
class TestWalkinStoreAndState(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.sys = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Walkin Store {uid}")
        self.tenant.checkin_code = f"WS{uid.upper()}"
        self.tenant.save(update_fields=["checkin_code"])
        self.co = State.objects.filter(code="CO").first() or State.objects.create(
            name="Colorado", code="CO", created_by=self.sys
        )
        self.retail = self.create_event_type("Retail Sampling", self.tenant)
        self.activation = self.create_event_type("Event Activation", self.tenant)
        self.on = datetime.date(2026, 10, 25)

    def _walkin(self, store_name, address, *, on=None, **kwargs):
        return checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name=store_name, address=address,
            on_date=on or self.on, actor=self.sys, event_type=self.retail, **kwargs,
        )

    def _prior(self, address, *, retailer=None, day=1):
        return Event.objects.create(
            tenant=self.tenant, name="Scheduled demo", address=address,
            date=dj_tz.make_aware(datetime.datetime(2026, 10, day, 12)),
            retailer=retailer, event_type=self.retail,
            created_by=self.sys, updated_by=self.sys,
        )

    # -- the typed store ----------------------------------------------------

    def test_typed_chain_creates_the_tenant_retailer_and_sets_the_state(self):
        event, created = self._walkin("king soopers parker", "17761 Cottonwood Dr, Parker, CO 80134")
        assert created
        event.refresh_from_db()
        assert event.retailer is not None and event.retailer.name == "King Soopers"
        assert event.retailer.tenant_id == self.tenant.id
        assert event.state_id == self.co.id

    def test_typed_name_reuses_the_tenants_own_retailer(self):
        savor = Retailer.objects.create(tenant=self.tenant, name="SAVOR", created_by=self.sys)
        event, _ = self._walkin("savor", "400 Central Ave, St Petersburg, FL 33701")
        event.refresh_from_db()
        assert event.retailer_id == savor.id
        assert Retailer.objects.filter(tenant=self.tenant).count() == 1

    def test_accepted_alias_is_honoured(self):
        kroger = Retailer.objects.create(tenant=self.tenant, name="Kroger", created_by=self.sys)
        RetailerAlias.objects.create(
            tenant=self.tenant, alias_key="the big k", alias_name="The Big K",
            canonical_name="Kroger", status=RetailerAlias.STATUS_ACCEPTED,
        )
        event, _ = self._walkin("The Big K", "1700 Monroe Dr NE, Atlanta, GA 30324")
        event.refresh_from_db()
        assert event.retailer_id == kroger.id

    def test_unknown_typed_store_sets_no_retailer(self):
        event, _ = self._walkin("Corner Mart", "12 Elm Rd, Boulder, CO 80301")
        event.refresh_from_db()
        assert event.retailer_id is None
        assert not Retailer.objects.filter(tenant=self.tenant).exists()

    # -- the same address ---------------------------------------------------

    def test_no_store_typed_falls_back_to_the_same_address(self):
        ks = Retailer.objects.create(tenant=self.tenant, name="King Soopers", created_by=self.sys)
        self._prior("8031 Wadsworth Boulevard, Arvada, CO 80003", retailer=ks)
        event, created = self._walkin("", "8031 Wadsworth Blvd")
        assert created
        event.refresh_from_db()
        assert event.retailer_id == ks.id
        assert event.state_id == self.co.id

    def test_two_stores_at_one_address_fill_nothing(self):
        ks = Retailer.objects.create(tenant=self.tenant, name="King Soopers", created_by=self.sys)
        sf = Retailer.objects.create(tenant=self.tenant, name="Safeway", created_by=self.sys)
        self._prior("8031 Wadsworth Boulevard, Arvada, CO 80003", retailer=ks, day=1)
        self._prior("8031 Wadsworth Blvd, Arvada, CO", retailer=sf, day=2)
        event, _ = self._walkin("", "8031 Wadsworth Blvd")
        event.refresh_from_db()
        assert event.retailer_id is None
        # Both places agree on the state, so that still fills.
        assert event.state_id == self.co.id

    def test_a_different_house_number_is_not_the_same_address(self):
        ks = Retailer.objects.create(tenant=self.tenant, name="King Soopers", created_by=self.sys)
        self._prior("8030 Wadsworth Boulevard, Arvada, CO 80003", retailer=ks)
        event, _ = self._walkin("", "8031 Wadsworth Blvd")
        event.refresh_from_db()
        assert event.retailer_id is None and event.state_id is None

    def test_an_explicit_retailer_is_never_overwritten(self):
        sf = Retailer.objects.create(tenant=self.tenant, name="Safeway", created_by=self.sys)
        first, _ = self._walkin("", "12 Elm Rd, Boulder, CO 80301")
        Event.objects.filter(pk=first.pk).update(retailer=sf)
        second, created = self._walkin("Kroger", "12 Elm Rd, Boulder, CO 80301")
        assert not created and second.id == first.id
        second.refresh_from_db()
        assert second.retailer_id == sf.id

    def test_a_later_ba_fills_a_blank_walkin_store(self):
        first, _ = self._walkin("", "12 Elm Rd, Boulder, CO 80301")
        assert Event.objects.get(pk=first.pk).retailer_id is None
        second, created = self._walkin("Kroger", "12 Elm Rd, Boulder, CO 80301")
        assert not created
        assert Event.objects.get(pk=first.pk).retailer.name == "Kroger"

    def test_the_recap_inherits_the_store_and_state(self):
        from recaps.models import CustomRecapTemplate, FileRecapCategory, FileType

        FileType.objects.get_or_create(name="image", defaults={"created_by": self.sys})
        FileRecapCategory.objects.create(name="Sampling photos", tenant=self.tenant, created_by=self.sys)
        template = CustomRecapTemplate.objects.create(
            tenant=self.tenant, name="Retail", event_type=self.retail, created_by=self.sys,
        )
        ba = self.create_ambassador(
            user=self.create_user(
                username="ba-ws@x.com", email="ba-ws@x.com", role=self.roles["ambassador"],
            ),
            is_active=False, created_by=self.sys,
        )
        event, _ = self._walkin("King Soopers", "17761 Cottonwood Dr, Parker, CO 80134")
        event = Event.objects.get(pk=event.pk)
        recap = checkin_web.submit_checkin_recap(
            event=event, ambassador=ba, template=template, field_values=[],
            files=[{"blobName": f"recap_files/checkin/{event.uuid}/a.jpg"}],
            total_engagements=None,
        )
        recap.refresh_from_db()
        assert recap.retailer.name == "King Soopers"
        assert recap.state_id == self.co.id

    # -- requiring the store ------------------------------------------------

    def test_program_classification(self):
        assert checkin_web.is_retail_sampling_type(self.retail)
        assert not checkin_web.is_retail_sampling_type(self.activation)
        seeding = self.create_event_type("Product Seeding", self.tenant)
        assert not checkin_web.is_retail_sampling_type(seeding)
        assert not checkin_web.is_retail_sampling_type(None)

    def test_new_retail_event_requires_a_store_name(self):
        with pytest.raises(ValueError, match="store name"):
            self._walkin("", "12 Elm Rd, Boulder, CO 80301", require_store=True)
        with pytest.raises(ValueError, match="store name"):
            # The address pasted into the store box is not a store name.
            self._walkin("12 Elm Rd", "12 Elm Rd, Boulder, CO 80301", require_store=True)
        assert not Event.objects.filter(tenant=self.tenant).exists()

    def test_joining_an_existing_event_does_not_ask(self):
        first, _ = self._walkin("Kroger", "12 Elm Rd, Boulder, CO 80301", require_store=True)
        second, created = self._walkin("", "12 Elm Rd, Boulder, CO 80301", require_store=True)
        assert not created and second.id == first.id

    def test_page_is_told_which_programs_need_a_store(self):
        self.tenant.checkin_event_type = self.retail
        self.tenant.save(update_fields=["checkin_event_type"])
        self.tenant.checkin_event_types.set([self.retail, self.activation])
        payload = checkin_web.build_tenant_context(self.tenant)
        assert payload["storeNameRequired"] is True
        flags = {t["name"]: t["storeNameRequired"] for t in payload["eventTypes"]}
        assert flags == {"Retail Sampling": True, "Event Activation": False}


@pytest.mark.django_db(transaction=True)
class TestIdentifyRequiresStoreName(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.sys = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"Identify Store {uid}")
        self.tenant.checkin_code = f"IS{uid.upper()}"
        self.retail = self.create_event_type("Retail Sampling", self.tenant)
        self.activation = self.create_event_type("Event Activation", self.tenant)
        self.tenant.checkin_event_type = self.retail
        self.tenant.save(update_fields=["checkin_code", "checkin_event_type"])
        self.tenant.checkin_event_types.set([self.retail, self.activation])
        State.objects.filter(code="CO").exists() or State.objects.create(
            name="Colorado", code="CO", created_by=self.sys
        )
        self.http = DjangoClient()

    def _identify(self, **extra):
        body = {
            "firstName": "Pat",
            "lastName": "Sampler",
            "phone": "5550142",
            "eventDate": dj_tz.localdate().isoformat(),
            "address": "17761 Cottonwood Dr, Parker, CO 80134",
        }
        body.update(extra)
        return self.http.post(
            reverse("events.public_checkin_identify", kwargs={"code": self.tenant.checkin_code}),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_retail_without_a_store_name_is_refused_in_plain_words(self):
        res = self._identify(eventTypeId=str(self.retail.id))
        assert res.status_code == 400, res.content
        assert res.json()["message"] == checkin_web.STORE_NAME_REQUIRED_ERROR
        assert not Event.objects.filter(tenant=self.tenant).exists()

    def test_retail_with_a_store_name_starts_and_sets_the_retailer(self):
        res = self._identify(eventTypeId=str(self.retail.id), storeName="King Soopers")
        assert res.status_code == 200, res.content
        event = Event.objects.get(uuid=res.json()["event"]["uuid"])
        assert event.retailer.name == "King Soopers"
        assert event.state.code == "CO"

    def test_activation_venue_name_stays_optional(self):
        res = self._identify(eventTypeId=str(self.activation.id))
        assert res.status_code == 200, res.content
