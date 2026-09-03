"""Tests for the PROGRAM SELECTOR on the standing check-in link.

Liquid Death runs Retail Sampling and Event Activation off one crew and one
link. A second link per program isn't available: `Tenant.checkin_code` is a
single column, so minting the second silently repoints the first. So the program
becomes a question the BA answers, and the answer is stamped on the event —
which is what `resolve_template_for_event` already matches templates on.

The load-bearing test here is
`test_two_programs_same_store_same_day_do_not_collapse`. Find-or-create used to
key on (tenant, address, date); with two programs selectable that key merges a
retail demo and an activation at one address on one day into a single event
carrying a single type, and the second BA is silently handed the first BA's
recap form. It submits cleanly against the wrong template, so nobody notices.
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
from events.models import Event


@pytest.mark.django_db(transaction=True)
class TestCheckinEventTypeSelector(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"LD Test {uid}")
        self.tenant.checkin_code = f"LD{uid.upper()}"
        self.tenant.save(update_fields=["checkin_code"])
        # Mirrors LD's real shape: retail is NOT the lowest-id type there, so a
        # lowest-id fallback would pick the wrong program.
        self.retail = self.create_event_type("Retail Sampling", self.tenant)
        self.activation = self.create_event_type("Event Activation", self.tenant)
        self.on = datetime.date(2026, 8, 1)
        self.addr = "1155 E State St, Trenton, NJ 08609, USA"

    def _offer_both(self):
        self.tenant.checkin_event_type = self.retail
        self.tenant.save(update_fields=["checkin_event_type"])
        self.tenant.checkin_event_types.set([self.retail, self.activation])

    # -- what the page is told ---------------------------------------------

    def test_no_types_configured_offers_nothing(self):
        """A brand that never opted in must look exactly as it does today —
        Total Wireless and Feel Free go through this path."""
        payload = checkin_web.build_tenant_context(self.tenant)
        assert payload["eventTypes"] == []

    def test_one_type_configured_still_offers_one(self):
        """One entry is not a selector. The page is expected to hide it rather
        than render a dropdown with a single option."""
        self.tenant.checkin_event_types.set([self.retail])
        payload = checkin_web.build_tenant_context(self.tenant)
        assert [t["name"] for t in payload["eventTypes"]] == ["Retail Sampling"]

    def test_both_types_are_offered_in_id_order(self):
        self._offer_both()
        payload = checkin_web.build_tenant_context(self.tenant)
        assert [t["name"] for t in payload["eventTypes"]] == [
            "Retail Sampling",
            "Event Activation",
        ]
        assert payload["eventTypes"][0]["id"] == str(self.retail.id)

    # -- resolving the BA's answer ------------------------------------------

    def test_a_valid_choice_resolves(self):
        self._offer_both()
        got = checkin_web.resolve_checkin_event_type(
            self.tenant, str(self.activation.id)
        )
        assert got is not None and got.id == self.activation.id

    def test_another_tenants_type_is_refused(self):
        """This id arrives from a PUBLIC endpoint. Resolving it unscoped would
        let anyone stamp another brand's type on this brand's event — and pull
        that brand's recap template through it."""
        other = self.create_tenant(name=f"Other {uuid.uuid4().hex[:6]}")
        foreign = self.create_event_type("Somebody Else's Program", other)
        assert (
            checkin_web.resolve_checkin_event_type(self.tenant, str(foreign.id))
            is None
        )

    @pytest.mark.parametrize("raw", ["", None, "abc", "999999999", "3 OR 1=1"])
    def test_junk_is_treated_as_unanswered(self, raw):
        assert checkin_web.resolve_checkin_event_type(self.tenant, raw) is None

    # -- the walk-in event key ---------------------------------------------

    def test_two_programs_same_store_same_day_do_not_collapse(self):
        """THE regression this widening exists for.

        Same tenant, same address, same date, DIFFERENT program → two events,
        each carrying its own type, so each BA gets their own recap form.
        """
        self._offer_both()
        retail_ev, made_retail = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="", address=self.addr,
            on_date=self.on, actor=self.system_user, event_type=self.retail,
        )
        act_ev, made_act = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="", address=self.addr,
            on_date=self.on, actor=self.system_user, event_type=self.activation,
        )
        assert made_retail is True and made_act is True
        assert retail_ev.id != act_ev.id
        assert retail_ev.event_type_id == self.retail.id
        assert act_ev.event_type_id == self.activation.id

    def test_same_program_same_store_same_day_still_shares_one_event(self):
        """The behaviour the old key existed to protect: a crew working one
        program at one store on one day stays on ONE event."""
        self._offer_both()
        a, made_a = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="A", address=self.addr,
            on_date=self.on, actor=self.system_user, event_type=self.retail,
        )
        b, made_b = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="B",
            address="1155 e state st,  trenton, nj 08609 usa",
            on_date=self.on, actor=self.system_user, event_type=self.retail,
        )
        assert made_a is True and made_b is False
        assert a.id == b.id
        assert Event.objects.filter(tenant=self.tenant).count() == 1

    def test_no_choice_falls_back_to_the_pinned_type(self):
        """An old page, or a curl with no program, must not break — and must
        not land on the arbitrary lowest-id type either."""
        self._offer_both()
        event, _ = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="", address=self.addr,
            on_date=self.on, actor=self.system_user,
        )
        assert event.event_type_id == self.retail.id

    def test_unpinned_brand_falls_back_to_the_first_offered_type(self):
        self.tenant.checkin_event_types.set([self.activation, self.retail])
        event, _ = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="", address=self.addr,
            on_date=self.on, actor=self.system_user,
        )
        # Offered in id order, so retail (created first) leads.
        assert event.event_type_id == self.retail.id

    # -- event naming -------------------------------------------------------

    def test_multi_program_events_are_named_apart(self):
        """Two events at one address on one day would otherwise be two
        identically-titled rows in the recap list."""
        self._offer_both()
        retail_ev, _ = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="", address="55 Elm St",
            on_date=self.on, actor=self.system_user, event_type=self.retail,
        )
        act_ev, _ = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="", address="55 Elm St",
            on_date=self.on, actor=self.system_user, event_type=self.activation,
        )
        assert retail_ev.name == "8/1/2026 - 55 Elm St · Retail Sampling"
        assert act_ev.name == "8/1/2026 - 55 Elm St · Event Activation"

    def test_a_previous_events_title_is_not_appended_as_a_store_name(self):
        """Caught on the live link.

        `recent_checkin_locations` hands the page {name: event.name, address:
        event.address}, and the store autocomplete copied that `name` into the
        store-name box. Event names are TITLES, never store names, so the second
        program's event came out called "8/2/2026 - 2 Test Way (8/2/2026 - 2 Test
        Way · Retail Sampling) · Event Activation". The page no longer copies
        such a name; this refuses it whatever the caller sends.
        """
        out = checkin_web.walkin_event_name(
            store_name="8/1/2026 - 123 Main St · Retail Sampling",
            address="123 Main St",
            on_date=self.on,
            program="Event Activation",
        )
        assert out == "8/1/2026 - 123 Main St · Event Activation"

    def test_a_real_store_name_still_rides_along(self):
        out = checkin_web.walkin_event_name(
            store_name="Kiosk 4", address="123 Main St", on_date=self.on,
            program="Retail Sampling",
        )
        assert out == "8/1/2026 - 123 Main St (Kiosk 4) · Retail Sampling"

    def test_single_program_titles_are_unchanged(self):
        """A brand running one program keeps today's exact title — no stray
        program suffix appearing on Total Wireless."""
        self.tenant.checkin_event_types.set([self.retail])
        event, _ = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="", address="55 Elm St",
            on_date=self.on, actor=self.system_user, event_type=self.retail,
        )
        assert event.name == "8/1/2026 - 55 Elm St"

    # -- per-program photo buckets ------------------------------------------
    #
    # The bucket CONFIG is per program (the shots a retail demo needs aren't the
    # shots an activation needs), while the category ROWS stay tenant-wide — a
    # recap belongs to one event and therefore one program, so a shared row is
    # never ambiguous in the PDF.

    def _category(self, name):
        from recaps.models import FileRecapCategory

        return FileRecapCategory.objects.create(
            tenant=self.tenant, name=name, created_by=self.system_user,
        )

    def _event(self, event_type):
        return Event.objects.create(
            tenant=self.tenant, name="x", address=self.addr,
            event_type=event_type, created_by=self.system_user,
        )

    def test_no_buckets_means_one_flat_photo_grid(self):
        """`[]` is the signal for "show the single unlabelled grid" — the shape
        every brand that hasn't opted in has today."""
        assert checkin_web.serialize_photo_buckets(self._event(self.retail)) == []

    def test_each_program_is_served_its_own_bucket_list(self):
        self._category("Table Set Up")
        self._category("Consumer Sampling Pictures")
        self._category("Activation Set Up")
        self._category("Expense Receipts (Parking)")
        self.tenant.checkin_photo_buckets = {
            "Retail Sampling": [
                {"name": "Table Set Up"},
                {"name": "Consumer Sampling Pictures", "min": 8},
            ],
            "Event Activation": [
                {"name": "Activation Set Up"},
                {"name": "Consumer Sampling Pictures", "min": 8},
                {"name": "Expense Receipts (Parking)"},
            ],
        }
        self.tenant.save(update_fields=["checkin_photo_buckets"])

        retail = checkin_web.serialize_photo_buckets(self._event(self.retail))
        activation = checkin_web.serialize_photo_buckets(self._event(self.activation))
        assert [b["name"] for b in retail] == [
            "Table Set Up",
            "Consumer Sampling Pictures",
        ]
        assert [b["name"] for b in activation] == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        # A bucket both programs want resolves to ONE row.
        assert retail[1]["id"] == activation[1]["id"]

    def test_a_program_key_matches_case_and_spacing_insensitively(self):
        self._category("Table Set Up")
        self.tenant.checkin_photo_buckets = {
            "retail-sampling": [{"name": "Table Set Up"}]
        }
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        buckets = checkin_web.serialize_photo_buckets(self._event(self.retail))
        assert [b["name"] for b in buckets] == ["Table Set Up"]

    def test_a_program_with_no_list_gets_the_plain_grid_not_another_programs(self):
        """Offering a retail BA an "Expense Receipts (Parking)" dropzone would
        be worse than offering them nothing."""
        self._category("Activation Set Up")
        self.tenant.checkin_photo_buckets = {
            "Event Activation": [{"name": "Activation Set Up"}]
        }
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        assert checkin_web.serialize_photo_buckets(self._event(self.retail)) == []

    def test_an_explicit_default_covers_the_other_programs(self):
        self._category("Sampling photos")
        self.tenant.checkin_photo_buckets = {
            "Event Activation": [],
            "default": [{"name": "Sampling photos"}],
        }
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        buckets = checkin_web.serialize_photo_buckets(self._event(self.retail))
        assert [b["name"] for b in buckets] == ["Sampling photos"]

    def test_the_other_programs_bucket_is_refused_on_submit(self):
        """A BA who switched programs on a page they left open would post an id
        this event's program never offered. It must fall back to the generic
        pile, not file an activation's parking receipt into a retail bucket.
        """
        from recaps.models import CustomRecapFile, CustomRecapTemplate, FileType

        photos = self._category("Sampling photos")  # the "1" sentinel target
        table = self._category("Table Set Up")
        parking = self._category("Expense Receipts (Parking)")
        self.tenant.checkin_photo_buckets = {
            "Retail Sampling": [{"name": "Table Set Up"}],
            "Event Activation": [{"name": "Expense Receipts (Parking)"}],
        }
        self.tenant.save(update_fields=["checkin_photo_buckets"])

        template = CustomRecapTemplate.objects.create(
            tenant=self.tenant, name="LD-Retail", event_type=self.retail,
            created_by=self.system_user,
        )
        FileType.objects.get_or_create(
            name="image", defaults={"created_by": self.system_user}
        )
        ba = self.create_ambassador(
            user=self.create_user(
                username="ba-xprog@x.com", email="ba-xprog@x.com",
                role=self.roles["ambassador"],
            ),
            is_active=False, created_by=self.system_user,
        )
        event = self._event(self.retail)

        recap = checkin_web.submit_checkin_recap(
            event=event, ambassador=ba, template=template, field_values=[],
            files=[
                {"blobName": f"recap_files/checkin/{event.uuid}/mine.jpg",
                 "category": str(table.id)},
                {"blobName": f"recap_files/checkin/{event.uuid}/theirs.jpg",
                 "category": str(parking.id)},
            ],
            total_engagements=None,
        )
        filed = {
            str(f.url.name).rsplit("/", 1)[-1]: f.file_recap_category.name
            for f in CustomRecapFile.objects.filter(custom_recap=recap)
        }
        assert filed == {
            "mine.jpg": "Table Set Up",       # this program's own bucket
            "theirs.jpg": "Sampling photos",  # the other program's — refused
        }
        assert photos.id and parking.id  # both rows exist; only one was usable

    def test_a_flat_list_still_applies_to_every_program(self):
        """The pre-selector shape. A single-program brand configured before this
        existed must not silently lose its buckets."""
        self._category("Table Set Up")
        self.tenant.checkin_photo_buckets = [{"name": "Table Set Up"}]
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        for etype in (self.retail, self.activation):
            buckets = checkin_web.serialize_photo_buckets(self._event(etype))
            assert [b["name"] for b in buckets] == ["Table Set Up"]


@pytest.mark.django_db(transaction=True)
class TestProductSeedingSkipsIdentifyLocation(AmbassadorsGraphQLTestCase):
    """Product Seeding: location + mileage live on the recap — identify must
    not 400 without a store address when that program is selected."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        cache.clear()
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"LD Seed {uid}")
        self.tenant.checkin_code = f"LD{uid.upper()}"
        self.tenant.save(update_fields=["checkin_code"])
        self.retail = self.create_event_type("Retail Sampling", self.tenant)
        self.activation = self.create_event_type("Event Activation", self.tenant)
        self.seeding = self.create_event_type("Product Seeding", self.tenant)
        self.tenant.checkin_event_type = self.retail
        self.tenant.save(update_fields=["checkin_event_type"])
        self.tenant.checkin_event_types.set(
            [self.retail, self.activation, self.seeding]
        )
        self.on = dj_tz.localdate()
        self.http = DjangoClient()

    def _identify(self, **extra):
        body = {
            "firstName": "Pat",
            "lastName": "Seeder",
            "phone": "5550199",
            "eventDate": self.on.isoformat(),
        }
        body.update(extra)
        return self.http.post(
            reverse(
                "events.public_checkin_identify",
                kwargs={"code": self.tenant.checkin_code},
            ),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_helpers_recognize_product_seeding(self):
        assert checkin_web.is_product_seeding_event_type(self.seeding) is True
        assert checkin_web.is_product_seeding_event_type(self.retail) is False
        addr = checkin_web.deferred_product_seeding_address(
            ambassador_id=42, on_date=self.on
        )
        assert "Product Seeding" in addr
        assert "42" in addr
        assert self.on.isoformat() in addr

    def test_seeding_identify_without_address_succeeds(self):
        res = self._identify(eventTypeId=str(self.seeding.id))
        assert res.status_code == 200, res.content
        payload = res.json()
        assert payload.get("sessionToken")
        assert payload["event"]["eventType"]["name"] == "Product Seeding"
        assert payload["event"]["address"].startswith("Product Seeding · BA ")
        ev = Event.objects.get(uuid=payload["event"]["uuid"])
        assert ev.event_type_id == self.seeding.id

    def test_retail_still_requires_address(self):
        res = self._identify(eventTypeId=str(self.retail.id))
        assert res.status_code == 400
        assert "address" in (res.json().get("message") or "").lower()

    def test_seeding_resume_reuses_deferred_event(self):
        first = self._identify(eventTypeId=str(self.seeding.id))
        assert first.status_code == 200
        addr = first.json()["event"]["address"]
        second = self._identify(
            eventTypeId=str(self.seeding.id), address=addr
        )
        assert second.status_code == 200
        assert first.json()["event"]["uuid"] == second.json()["event"]["uuid"]
