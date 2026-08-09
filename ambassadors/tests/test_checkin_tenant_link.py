"""Tests for the STANDING tenant-wide web check-in link.

The per-event link (`/checkin/<walkup_code>`) needs an activation to exist
first. A tenant's `checkin_code` is the standing twin: one durable link, pinned
on the client's page, where the BA supplies the store + date and Spark
finds-or-creates the event.

The behaviour that actually matters to the field — and the reason
find-or-create is keyed on (tenant, normalized address, date) rather than on the
BA — is that SEVERAL BAs working one location on one day must land on ONE event,
each with their own booking. That is what `test_two_bas_same_store_same_day_share_one_event`
pins down.
"""
import uuid

import pytest

from ambassadors import checkin_web
from ambassadors.models import AmbassadorEvent
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Event


@pytest.mark.django_db(transaction=True)
class TestTenantCheckinLink(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.system_user = self.get_system_user()
        self.roles = self.setup_default_roles()
        uid = str(uuid.uuid4())[:8]
        self.tenant = self.create_tenant(name=f"TW Test {uid}")
        self.tenant.checkin_code = f"TW{uid.upper()}"
        self.tenant.save(update_fields=["checkin_code"])

    # -- resolution ---------------------------------------------------------

    def test_tenant_code_resolves_to_tenant(self):
        kind, target = checkin_web.resolve_checkin_target(self.tenant.checkin_code)
        assert kind == "tenant"
        assert target.id == self.tenant.id

    def test_tenant_code_is_case_insensitive(self):
        kind, target = checkin_web.resolve_checkin_target(
            self.tenant.checkin_code.lower()
        )
        assert kind == "tenant"
        assert target.id == self.tenant.id

    def test_unknown_code_resolves_to_nothing(self):
        kind, target = checkin_web.resolve_checkin_target("NOPE-NOT-A-CODE")
        assert kind is None and target is None

    def test_event_codes_still_win(self):
        """An existing per-event link must keep its exact behaviour — event
        codes are tried first, so a tenant code can never shadow one."""
        event = Event.objects.create(
            tenant=self.tenant, name="Pinned event", address="1 Test Way",
            walkup_code="EVT12345", created_by=self.system_user,
        )
        kind, target = checkin_web.resolve_checkin_target("EVT12345")
        assert kind == "event"
        assert target.id == event.id

    # -- find-or-create -----------------------------------------------------

    def test_creates_event_on_first_checkin(self):
        import datetime

        event, created = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant,
            store_name="Total Wireless Trenton",
            address="1155 E State St, Trenton, NJ 08609, USA",
            on_date=datetime.date(2026, 7, 31), actor=self.system_user,
        )
        assert created is True
        assert event.tenant_id == self.tenant.id
        # Titled date-then-address so ops can tell WHICH stop this was;
        # the store name rides along in parentheses.
        assert event.name == (
            "7/31/2026 - 1155 E State St, Trenton, NJ 08609, USA "
            "(Total Wireless Trenton)"
        )
        # Stored at noon UTC so the calendar date reads correctly in every US
        # zone — midnight would report the previous evening.
        assert event.date.hour == 12
        assert event.date.date() == datetime.date(2026, 7, 31)

    def test_two_bas_same_store_same_day_share_one_event(self):
        """The multi-BA case: same store, same day, different spellings of the
        address — one event, two bookings."""
        import datetime

        on = datetime.date(2026, 7, 31)
        first, created_a = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="TW Trenton",
            address="1155 E State St, Trenton, NJ 08609, USA", on_date=on, actor=self.system_user,
        )
        second, created_b = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="Total Wireless",
            address="1155 e state st,  trenton, nj 08609 usa", on_date=on, actor=self.system_user,
        )
        assert created_a is True
        assert created_b is False, "second BA must JOIN, not fork a new event"
        assert first.id == second.id
        assert Event.objects.filter(tenant=self.tenant).count() == 1

        amb_a = self.create_ambassador(
            user=self.create_user(
                username="ba_a@x.com", email="ba_a@x.com",
                role=self.roles["ambassador"],
            ),
            is_active=False, created_by=self.system_user,
        )
        amb_b = self.create_ambassador(
            user=self.create_user(
                username="ba_b@x.com", email="ba_b@x.com",
                role=self.roles["ambassador"],
            ),
            is_active=False, created_by=self.system_user,
        )
        checkin_web.ensure_walkup_booking(first, amb_a, actor=amb_a.user)
        checkin_web.ensure_walkup_booking(second, amb_b, actor=amb_b.user)

        bookings = AmbassadorEvent.objects.filter(event=first)
        assert bookings.count() == 2, "each BA gets their own booking"
        assert all(not b.is_approved for b in bookings), (
            "web check-ins stay pending until an admin confirms them, so "
            "nothing counts in KPI/payroll before review"
        )

    def test_different_store_same_day_makes_a_second_event(self):
        import datetime

        on = datetime.date(2026, 7, 31)
        a, _ = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="A",
            address="1155 E State St, Trenton, NJ", on_date=on, actor=self.system_user,
        )
        b, created = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="B",
            address="58 E Lake St, Chicago, IL", on_date=on, actor=self.system_user,
        )
        assert created is True
        assert a.id != b.id

    def test_same_store_different_day_makes_a_second_event(self):
        import datetime

        addr = "1155 E State St, Trenton, NJ"
        a, _ = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="A", address=addr,
            on_date=datetime.date(2026, 7, 31), actor=self.system_user,
        )
        b, created = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="A", address=addr,
            on_date=datetime.date(2026, 8, 1), actor=self.system_user,
        )
        assert created is True
        assert a.id != b.id

    def test_blank_address_is_refused(self):
        import datetime

        with pytest.raises(ValueError):
            checkin_web.find_or_create_walkin_event(
                tenant=self.tenant, store_name="No address", address="   ",
                on_date=datetime.date(2026, 7, 31), actor=self.system_user,
            )

    # -- context ------------------------------------------------------------

    def test_tenant_context_asks_for_event_details(self):
        payload = checkin_web.build_tenant_context(self.tenant)
        assert payload["mode"] == "tenant"
        assert payload["needsEventDetails"] is True
        assert payload["brand"]["name"] == self.tenant.name
        assert isinstance(payload["recentLocations"], list)

    def test_recent_locations_dedupe_by_normalized_address(self):
        Event.objects.create(
            tenant=self.tenant, name="TW A", address="1155 E State St, Trenton, NJ",
            created_by=self.system_user,
        )
        Event.objects.create(
            tenant=self.tenant, name="TW A again", address="1155 e state st,  trenton, nj",
            created_by=self.system_user,
        )
        Event.objects.create(
            tenant=self.tenant, name="TW B", address="58 E Lake St, Chicago, IL",
            created_by=self.system_user,
        )
        out = checkin_web.recent_checkin_locations(self.tenant)
        keys = {checkin_web.normalize_place(x["address"]) for x in out}
        assert len(keys) == len(out), "no duplicate stores in the autocomplete"
        assert len(out) == 2

    # -- event naming -------------------------------------------------------

    def test_event_is_titled_date_then_address(self):
        """The title is what ops reads on the recap, in pickers and exports.
        Every Total Wireless walk-in used to come through titled "Total
        wireless" — the brand, telling you nothing about WHICH stop."""
        import datetime

        assert checkin_web.walkin_event_name(
            store_name="", address="7902 Taconite Drive, Sparks, NV 89436",
            on_date=datetime.date(2026, 8, 1),
        ) == "8/1/2026 - 7902 Taconite Drive, Sparks, NV 89436"

    def test_a_useful_store_name_is_kept(self):
        import datetime

        out = checkin_web.walkin_event_name(
            store_name="Kiosk 4", address="123 Main St",
            on_date=datetime.date(2026, 8, 1),
        )
        assert out == "8/1/2026 - 123 Main St (Kiosk 4)"

    def test_a_store_name_already_in_the_address_is_not_repeated(self):
        import datetime

        out = checkin_web.walkin_event_name(
            store_name="Main St", address="123 Main St",
            on_date=datetime.date(2026, 8, 1),
        )
        assert out == "8/1/2026 - 123 Main St"

    def test_naming_does_not_change_the_find_or_create_key(self):
        """Renaming must never fork or merge events — the key is
        (tenant, normalized address, date), not the name."""
        import datetime

        on = datetime.date(2026, 7, 31)
        a, created_a = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="Kiosk 4",
            address="55 Elm St", on_date=on, actor=self.system_user,
        )
        b, created_b = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="totally different name",
            address="55 elm st", on_date=on, actor=self.system_user,
        )
        assert created_a is True and created_b is False
        assert a.id == b.id
        assert a.name.startswith("7/31/2026 - 55 Elm St")

    # -- fuzzy address matching --------------------------------------------
    # A scheduled event carries an admin-TYPED address ("1201 Avocado Ave"); a
    # walk-in's address is REVERSE-GEOCODED from the BA's GPS ("1201 Avocado
    # Boulevard, El Cajon, CA 92020"). normalize_place keeps those distinct, so
    # the walk-in forked a duplicate event and the scheduled row read DUE. The
    # address_core_key collapses street-suffix + ZIP so they connect.

    def test_address_core_key_collapses_suffix_and_zip(self):
        typed = "1201 Avocado Ave, El Cajon, CA"
        geocoded = "1201 Avocado Boulevard, El Cajon, CA 92020"
        assert (
            checkin_web.address_core_key(typed)
            == checkin_web.address_core_key(geocoded)
            == "1201 avocado el cajon ca"
        )
        # No leading street number -> untrusted (caller falls back to strict).
        assert checkin_web.address_core_key("Avocado Plaza, El Cajon") == ""
        # A 5-digit STREET NUMBER is kept; only trailing ZIPs are dropped.
        assert checkin_web.address_core_key("12345 Main St").startswith("12345 ")

    def test_walkin_connects_across_suffix_and_zip_variance(self):
        # Regression for the Vons "DUE" bug: "…Ave" and reverse-geocoded
        # "…Boulevard …92020" must land on ONE event, not fork a duplicate.
        import datetime

        on = datetime.date(2026, 8, 7)
        scheduled, made_a = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="Vons",
            address="1201 Avocado Ave, El Cajon, CA",
            on_date=on, actor=self.system_user,
        )
        walkin, made_b = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="Vons El Cajon",
            address="1201 Avocado Boulevard, El Cajon, CA 92020",
            on_date=on, actor=self.system_user,
        )
        assert made_a is True and made_b is False
        assert scheduled.id == walkin.id

    def test_fuzzy_match_does_not_merge_different_addresses(self):
        # The looser key stays tight: a different street NUMBER is a different
        # place and must NOT collapse.
        import datetime

        on = datetime.date(2026, 8, 7)
        a, made_a = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="A",
            address="1201 Avocado Blvd, El Cajon, CA",
            on_date=on, actor=self.system_user,
        )
        b, made_b = checkin_web.find_or_create_walkin_event(
            tenant=self.tenant, store_name="B",
            address="1300 Avocado Blvd, El Cajon, CA",
            on_date=on, actor=self.system_user,
        )
        assert made_a is True and made_b is True
        assert a.id != b.id
