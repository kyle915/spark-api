"""Tests for the `set_tenant_event_types` standardization command.

Covers the "swap stock, keep custom" policy Kyle picked (2026-06-13):
ensure Retail Sampling / On-Premise Sampling / Event exist with Retail
Sampling default, retire only the legacy stock types (repointing their
events first), keep custom types, and never touch excluded tenants (Jeeter).
"""
from __future__ import annotations

import pytest
from django.core.management import call_command

from events.models import Event, EventType
from events.tests.base import EventsGraphQLTestCase

OWNER_EMAIL = "system@spark.local"  # get_system_user()'s email
STOCK = ["Sampling", "Promotion", "Launch", "Special Event"]
STANDARD = {"Retail Sampling", "On-Premise Sampling", "Event"}


@pytest.mark.django_db(transaction=True)
class TestSetTenantEventTypes(EventsGraphQLTestCase):
    def _seed_stock(self, tenant):
        """Seed the old stock defaults (Sampling is the default), like a
        freshly-created tenant."""
        for name in STOCK:
            self.create_event_type(
                name, tenant, slug=name.lower().replace(" ", "-"),
                is_default=(name == "Sampling"),
            )

    def _names(self, tenant) -> set[str]:
        return set(
            EventType.objects.filter(tenant=tenant).values_list("name", flat=True)
        )

    def test_swap_stock_keeps_custom_and_fixes_default(self):
        tenant = self.create_tenant("Acme")
        self._seed_stock(tenant)
        # A client-specific type that must survive.
        self.create_event_type("Event Activation", tenant, slug="event-activation")

        call_command(
            "set_tenant_event_types",
            tenant_name="Acme",
            owner_email=OWNER_EMAIL,
            commit=True,
        )

        names = self._names(tenant)
        assert names == STANDARD | {"Event Activation"}
        for stock in STOCK:
            assert not EventType.objects.filter(
                tenant=tenant, name=stock
            ).exists(), f"{stock} should have been retired"
        defaults = list(
            EventType.objects.filter(tenant=tenant, is_default=True).values_list(
                "name", flat=True
            )
        )
        assert defaults == ["Retail Sampling"]

    def test_repoints_events_off_retired_type(self):
        tenant = self.create_tenant("Beta")
        self._seed_stock(tenant)
        sampling = EventType.objects.get(tenant=tenant, name="Sampling")
        evt = Event.objects.create(
            name="Kroger demo",
            tenant=tenant,
            address="1 Main St",
            created_by=self.get_system_user(),
            event_type=sampling,
        )

        call_command(
            "set_tenant_event_types",
            tenant_name="Beta",
            owner_email=OWNER_EMAIL,
            commit=True,
        )

        evt.refresh_from_db()
        assert evt.event_type.name == "Retail Sampling"
        assert not EventType.objects.filter(tenant=tenant, name="Sampling").exists()

    def test_excludes_jeeter_but_standardizes_others(self):
        jeeter = self.create_tenant("Jeeter")
        self._seed_stock(jeeter)
        acme = self.create_tenant("Acme")
        self._seed_stock(acme)

        call_command(
            "set_tenant_event_types",
            all_tenants=True,
            owner_email=OWNER_EMAIL,
            commit=True,
        )

        # Jeeter untouched: still the stock set, no standardization.
        assert self._names(jeeter) == set(STOCK)
        # Acme standardized.
        assert self._names(acme) == STANDARD

    def test_dry_run_writes_nothing(self):
        tenant = self.create_tenant("Gamma")
        self._seed_stock(tenant)

        call_command(
            "set_tenant_event_types",
            tenant_name="Gamma",
            owner_email=OWNER_EMAIL,
            commit=False,
        )

        assert self._names(tenant) == set(STOCK)
        assert not EventType.objects.filter(
            tenant=tenant, name="Retail Sampling"
        ).exists()
