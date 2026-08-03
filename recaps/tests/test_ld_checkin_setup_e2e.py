"""End-to-end run of the LD check-in seeder against an LD-shaped fixture.

The unit tests cover the pieces; this covers the thing the pieces have to add up
to, because the seeder's writes have to AGREE with each other and with what the
page is served. Its failure mode is silent — a bucket configured but not backed
by a category, or a program made selectable whose template never got the SKU
picker, produces no error and a wrong recap.

The fixture mirrors production: five event types, both templates already present
(the command creates neither), and the seeded default categories already there
including the "Table setup" row the retail bucket has to relabel rather than
duplicate, and the two sentinel rows it must leave alone.
"""
import io

import pytest
from django.core.management import call_command

from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestSeederEndToEnd(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events.models import EventType
        from recaps.models import CustomRecapTemplate, FileRecapCategory

        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.tenant = self.create_tenant(name="Liquid Death Fixture")
        # LD's real order: retail first by id, then the others.
        self.types = {}
        for name in (
            "Retail Sampling",
            "Direct Event Sampling",
            "Event Activation",
            "On-Premise Sampling",
            "Event",
        ):
            self.types[name] = EventType.objects.create(
                name=name, tenant=self.tenant, created_by=self.user
            )
        self.tpl_act = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Liquid Death-Event Activation",
            event_type=self.types["Event Activation"],
            created_by=self.user,
        )
        self.tpl_retail = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Liquid Death-Retail Sampling",
            event_type=self.types["Retail Sampling"],
            created_by=self.user,
        )
        for name in ("Sampling photos", "Table setup", "Receipts"):
            FileRecapCategory.objects.create(
                name=name, tenant=self.tenant, created_by=self.user
            )

    def _run(self, **kw):
        out = io.StringIO()
        call_command(
            "setup_ld_retail_checkin",
            tenant="liquid death fixture",
            stdout=out,
            **kw,
        )
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        from recaps.models import CustomField, FileRecapCategory

        log = self._run()
        print(log)
        assert "DRY RUN" in log
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code is None
        assert self.tenant.checkin_photo_buckets is None
        assert self.tenant.checkin_event_types.count() == 0
        assert CustomField.objects.filter(
            custom_recap_template__tenant=self.tenant
        ).count() == 0
        assert FileRecapCategory.objects.filter(tenant=self.tenant).count() == 3

    def test_apply_wires_both_programs(self):
        from ambassadors import checkin_web
        from events.models import Event
        from recaps.models import CustomField, FileRecapCategory

        log = self._run(apply=True)
        print(log)
        self.tenant.refresh_from_db()

        # 1. code minted
        assert (self.tenant.checkin_code or "").startswith("LD-")
        # 2. both programs selectable, retail pinned as fallback
        offered = [t.name for t in checkin_web.selectable_event_types(self.tenant)]
        assert offered == ["Retail Sampling", "Event Activation"]
        assert self.tenant.checkin_event_type.name == "Retail Sampling"
        # 3. Products Sampled on BOTH templates, 31 options each
        for tpl in (self.tpl_retail, self.tpl_act):
            f = CustomField.objects.get(
                custom_recap_template=tpl, name="Products Sampled"
            )
            assert len(f.options) == 31
            assert "Iced Tea — Sweet Reaper" in f.options
            assert f.required is False
        # 4. buckets per program, "Table setup" relabelled not duplicated
        names = set(
            FileRecapCategory.objects.filter(tenant=self.tenant).values_list(
                "name", flat=True
            )
        )
        assert "Table Set Up" in names and "Table setup" not in names
        assert "Sampling photos" in names and "Receipts" in names  # sentinels safe
        retail = checkin_web.serialize_photo_buckets(
            Event(tenant=self.tenant, event_type=self.types["Retail Sampling"])
        )
        activation = checkin_web.serialize_photo_buckets(
            Event(tenant=self.tenant, event_type=self.types["Event Activation"])
        )
        assert [b["name"] for b in retail] == [
            "Table Set Up",
            "Product Display",
            "Consumer Sampling Pictures",
            "Product Receipt",
        ]
        assert [b["name"] for b in activation] == [
            "Activation Set Up",
            "Consumer Sampling Pictures",
            "Expense Receipts (Parking)",
        ]
        assert retail[2]["id"] == activation[1]["id"]
        # 5. a program NOT on the link gets no buckets
        assert (
            checkin_web.serialize_photo_buckets(
                Event(tenant=self.tenant, event_type=self.types["Event"])
            )
            == []
        )
        # 6. the tenant context offers both, so the FE renders a selector
        ctx = checkin_web.build_tenant_context(self.tenant)
        assert [t["name"] for t in ctx["eventTypes"]] == [
            "Retail Sampling",
            "Event Activation",
        ]

    def test_apply_is_idempotent(self):
        from recaps.models import CustomField, FileRecapCategory

        self._run(apply=True)
        self.tenant.refresh_from_db()
        code = self.tenant.checkin_code
        cats = sorted(
            FileRecapCategory.objects.filter(tenant=self.tenant).values_list(
                "name", flat=True
            )
        )
        cfg = self.tenant.checkin_photo_buckets
        fields = CustomField.objects.filter(
            custom_recap_template__tenant=self.tenant
        ).count()

        self._run(apply=True)
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == code
        assert self.tenant.checkin_photo_buckets == cfg
        assert (
            sorted(
                FileRecapCategory.objects.filter(tenant=self.tenant).values_list(
                    "name", flat=True
                )
            )
            == cats
        )
        assert (
            CustomField.objects.filter(
                custom_recap_template__tenant=self.tenant
            ).count()
            == fields
        )
        assert self.tenant.checkin_event_types.count() == 2

    def test_forced_code_keeps_an_already_shared_link(self):
        self._run(apply=True, code="LD-TNBJ8K")
        self.tenant.refresh_from_db()
        assert self.tenant.checkin_code == "LD-TNBJ8K"
