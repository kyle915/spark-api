"""Breakaway Jimmy Johns / Hiyo recaps: seeder shape + walk-up apply.

Pins the templates to the client's own PDFs — "Jimmy Johns // Breakaway
Music Festival" (Tishawna Banks, #8, 05/31/2026) and "Hiyo // Breakaway
Music Festival" (Samantha Redmond, #4, 06/27/2026) — so a future edit is
deliberate, and proves the standing-link command can create the tenant from
scratch with the Jimmy Johns vs Hiyo picker on one code.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from recaps.management.commands.setup_breakaway_checkin import (
    ALL_BUCKETS,
    CODE_PREFIX,
    HIYO_PROGRAM,
    HIYO_SPEC,
    HIYO_TEMPLATE_NAME,
    JJ_PROGRAM,
    JJ_SPEC,
    JJ_TEMPLATE_NAME,
    PHOTO_BUCKETS_BY_PROGRAM,
    TENANT_NAME,
    TENANT_SLUG,
)
from tenants.tests.base import BaseGraphQLTestCase

VALID_SECRET = "test-cron-secret-value-only-for-tests"
BREAKAWAY_CRON_URL = "/internal/cron/setup-breakaway-checkin"

EXCLUDED = (
    "date",
    "date:",
    "today",
)

JJ_FIELD_NAMES = [f[0] for _, fields in JJ_SPEC for f in fields]
HIYO_FIELD_NAMES = [f[0] for _, fields in HIYO_SPEC for f in fields]


class TestBreakawaySpec:
    def test_jimmy_johns_covers_the_pdf_minus_date(self):
        assert JJ_FIELD_NAMES == [
            "Festival Location",
            "Total bags of chips set out today (estimate is fine)",
            "Total bags remaining at end of shift (estimate)",
            "Total number of chips distributed",
            "How many people did you personally interact with about the brand / chips / disco?",
            "During your shift, when were chips moving fastest?",
            "When were chips moving slowest?",
            "What Worked Well?",
            "What Could Be Improved?",
            "Anything that the client MUST know in the recap (wins, concerns, important learning)",
        ]

    def test_hiyo_covers_the_pdf_minus_date(self):
        assert HIYO_FIELD_NAMES == [
            "Festival Location",
            "Total samples distributed",
            "Estimated foot traffic / impressions",
            "Key highlight (1–2 sentences) / What worked well & What didn't?",
            "What were some consumer comments that you heard?",
            "What percent of consumer had heard of or tried Hiyo before?",
        ]

    def test_both_brands_have_open_festival_location_text(self):
        """Kyle: Festival Location is typed city/state, not a venue list."""
        for names in (JJ_FIELD_NAMES, HIYO_FIELD_NAMES):
            assert "Festival Location" in names
        for spec in (JJ_SPEC, HIYO_SPEC):
            field = next(
                f for _, fields in spec for f in fields if f[0] == "Festival Location"
            )
            assert field[1] == "text"
            assert field[2] is True
            assert field[3] == []

    def test_date_is_not_on_either_form(self):
        for names in (JJ_FIELD_NAMES, HIYO_FIELD_NAMES):
            lowered = [n.lower() for n in names]
            for banned in EXCLUDED:
                assert not any(
                    n == banned or n.startswith(banned + " ") for n in lowered
                )

    def test_photo_buckets_match_the_pdf_labels(self):
        assert [b["name"] for b in PHOTO_BUCKETS_BY_PROGRAM[JJ_PROGRAM]] == [
            "Activation / Sampling / Recap Photos"
        ]
        assert [b["name"] for b in PHOTO_BUCKETS_BY_PROGRAM[HIYO_PROGRAM]] == [
            "Consumer Sampling Pictures"
        ]
        assert ALL_BUCKETS == [
            {"name": "Activation / Sampling / Recap Photos"},
            {"name": "Consumer Sampling Pictures"},
        ]

    def test_kinds_are_canonical_and_not_image(self):
        """Photos are buckets, not template image fields — two image
        fields would share one grid on the walk-up page."""
        kinds = {f[1] for spec in (JJ_SPEC, HIYO_SPEC) for _, fields in spec for f in fields}
        assert kinds <= {"text", "number", "longtext", "select", "multiselect"}
        assert "image" not in kinds

    def test_code_prefix_is_brand_scoped(self):
        assert CODE_PREFIX == "BRK-"
        assert JJ_TEMPLATE_NAME.startswith("Breakaway")
        assert HIYO_TEMPLATE_NAME.startswith("Breakaway")
        assert TENANT_SLUG == "breakaway"
        assert TENANT_NAME == "Breakaway"


@pytest.mark.django_db(transaction=True)
class TestBreakawaySetupCommand(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.user = self.get_system_user()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])

    def _run(self, **kw):
        out = io.StringIO()
        call_command("setup_breakaway_checkin", stdout=out, **kw)
        return out.getvalue()

    def test_dry_run_creates_nothing(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        log = self._run(tenant="breakaway")
        assert "DRY-RUN" in log
        assert TENANT_NAME in log
        assert not Tenant.objects.filter(slug=TENANT_SLUG).exists()
        assert not CustomRecapTemplate.objects.filter(
            name=JJ_TEMPLATE_NAME
        ).exists()

    def test_apply_creates_tenant_templates_code_and_buckets(self):
        from events.models import EventType
        from recaps.models import CustomField, CustomRecapTemplate, FileRecapCategory
        from tenants.models import Tenant

        log = self._run(tenant="breakaway", apply=True)
        assert "APPLIED" in log
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        assert tenant.name == TENANT_NAME
        assert tenant.checkin_code and tenant.checkin_code.startswith("BRK-")
        assert tenant.checkin_location_mode == Tenant.CHECKIN_LOCATION_ADDRESS
        assert tenant.checkin_photo_buckets == PHOTO_BUCKETS_BY_PROGRAM
        assert tenant.checkin_event_type is not None
        assert tenant.checkin_event_type.name == JJ_PROGRAM
        assert set(tenant.checkin_event_types.values_list("name", flat=True)) == {
            JJ_PROGRAM,
            HIYO_PROGRAM,
        }
        assert {JJ_PROGRAM, HIYO_PROGRAM} <= set(
            EventType.objects.filter(tenant=tenant).values_list("name", flat=True)
        )
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Activation / Sampling / Recap Photos"
        ).exists()
        assert FileRecapCategory.objects.filter(
            tenant=tenant, name="Consumer Sampling Pictures"
        ).exists()

        jj_tpl = CustomRecapTemplate.objects.get(tenant=tenant, name=JJ_TEMPLATE_NAME)
        hiyo_tpl = CustomRecapTemplate.objects.get(
            tenant=tenant, name=HIYO_TEMPLATE_NAME
        )
        jj_names = list(
            CustomField.objects.filter(custom_recap_template=jj_tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        hiyo_names = list(
            CustomField.objects.filter(custom_recap_template=hiyo_tpl)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert jj_names == JJ_FIELD_NAMES
        assert hiyo_names == HIYO_FIELD_NAMES
        assert jj_tpl.event_type.name == JJ_PROGRAM
        assert hiyo_tpl.event_type.name == HIYO_PROGRAM

        code = tenant.checkin_code
        log2 = self._run(tenant="breakaway", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log2

    def test_existing_tenant_keeps_its_other_event_types(self):
        """Breakaway already exists with history — nothing gets retired."""
        from events.models import Event, EventType
        from tenants.models import Tenant

        self._run(tenant="breakaway", apply=True)
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        legacy = EventType.objects.create(
            name="Jimmy Johns Silent DJ",
            slug="jimmy-johns-silent-dj",
            tenant=tenant,
            created_by=self.user,
            is_default=False,
        )
        Event.objects.create(
            name="Silent DJ Jimmy Johns-05/15/2026",
            tenant=tenant,
            address="Center Parc Stadium - Atlanta, GA",
            event_type=legacy,
            created_by=self.user,
        )

        log = self._run(tenant="breakaway", apply=True)
        tenant.refresh_from_db()
        assert "retire" not in log.lower()
        assert EventType.objects.filter(pk=legacy.pk).exists()
        # The legacy near-name is NOT adopted as the brand program.
        assert tenant.checkin_event_type.name == JJ_PROGRAM
        assert set(tenant.checkin_event_types.values_list("name", flat=True)) == {
            JJ_PROGRAM,
            HIYO_PROGRAM,
        }

    def test_reapply_keeps_both_programs_and_the_code(self):
        from recaps.models import CustomRecapTemplate
        from tenants.models import Tenant

        self._run(tenant="breakaway", apply=True)
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        code = tenant.checkin_code

        log = self._run(tenant="breakaway", apply=True)
        tenant.refresh_from_db()
        assert tenant.checkin_code == code
        assert "already set" in log
        assert tenant.checkin_event_type.name == JJ_PROGRAM
        assert CustomRecapTemplate.objects.filter(tenant=tenant).count() == 2

    def _replace_jj_template_with_leftover(self, tenant, with_recap: bool):
        """Reproduce the leftover-template race: a same-event-type template
        with a lower id would win resolve_template_for_event over the PDF
        form until folded."""
        from ambassadors.checkin_web import resolve_template_for_event
        from events.models import Event
        from recaps.models import CustomField, CustomRecap, CustomRecapTemplate

        jj_tpl = CustomRecapTemplate.objects.get(
            tenant=tenant, name=JJ_TEMPLATE_NAME
        )
        event_type = jj_tpl.event_type
        CustomField.objects.filter(custom_recap_template=jj_tpl).delete()
        jj_tpl.delete()
        leftover = CustomRecapTemplate.objects.create(
            tenant=tenant,
            name="Breakaway - Jimmy Johns Recap",
            event_type=event_type,
            product_samples=False,
            sales_performance=False,
            layout={},
            created_by=self.user,
        )
        recap = None
        event = Event.objects.create(
            name="Breakaway leftover event",
            tenant=tenant,
            address="1 Main St",
            event_type=event_type,
            created_by=self.user,
        )
        if with_recap:
            recap = CustomRecap.objects.create(
                name="old jj recap",
                event=event,
                tenant=tenant,
                custom_recap_template=leftover,
                created_by=self.user,
            )
        return leftover, event, recap, resolve_template_for_event

    def test_reapply_deletes_unused_leftover_so_walkup_hits_pdf(self):
        from recaps.models import CustomField, CustomRecapTemplate
        from tenants.models import Tenant

        self._run(tenant="breakaway", apply=True)
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        leftover, event, _, resolve = self._replace_jj_template_with_leftover(
            tenant, with_recap=False
        )
        leftover_id = leftover.id
        assert resolve(event).id == leftover_id

        log = self._run(tenant="breakaway", apply=True)
        assert "deleted" in log
        assert not CustomRecapTemplate.objects.filter(pk=leftover_id).exists()
        pdf = CustomRecapTemplate.objects.get(tenant=tenant, name=JJ_TEMPLATE_NAME)
        assert CustomRecapTemplate.objects.filter(
            tenant=tenant, event_type=pdf.event_type
        ).count() == 1
        event.refresh_from_db()
        picked = resolve(event)
        assert picked.id == pdf.id
        names = list(
            CustomField.objects.filter(custom_recap_template=picked)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert names == JJ_FIELD_NAMES

    def test_reapply_folds_leftover_with_recaps_into_pdf_name(self):
        from recaps.models import CustomField, CustomRecap, CustomRecapTemplate
        from tenants.models import Tenant

        self._run(tenant="breakaway", apply=True)
        tenant = Tenant.objects.get(slug=TENANT_SLUG)
        leftover, event, recap, resolve = self._replace_jj_template_with_leftover(
            tenant, with_recap=True
        )
        leftover_id = leftover.id

        log = self._run(tenant="breakaway", apply=True)
        assert "renamed leftover" in log
        leftover.refresh_from_db()
        assert leftover.id == leftover_id
        assert leftover.name == JJ_TEMPLATE_NAME
        recap.refresh_from_db()
        assert recap.custom_recap_template_id == leftover_id
        assert CustomRecap.objects.filter(pk=recap.id).exists()
        event.refresh_from_db()
        picked = resolve(event)
        assert picked.id == leftover_id
        names = list(
            CustomField.objects.filter(custom_recap_template=leftover)
            .order_by("recap_section__order", "order", "id")
            .values_list("name", flat=True)
        )
        assert names == JJ_FIELD_NAMES


@pytest.mark.django_db
class TestBreakawaySetupCronView:
    def test_valid_secret_fires_command(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    BREAKAWAY_CRON_URL,
                    {"tenant": "breakaway"},
                    HTTP_X_CRON_SECRET=VALID_SECRET,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["apply"] is False
        mock_call.assert_called_once()
        assert mock_call.call_args[0][0] == "setup_breakaway_checkin"

    def test_bad_secret_returns_401(self):
        client = Client()
        with override_settings(INTERNAL_CRON_SECRET=VALID_SECRET):
            from unittest.mock import patch

            with patch("digest.cron_views.call_command") as mock_call:
                resp = client.post(
                    BREAKAWAY_CRON_URL,
                    HTTP_X_CRON_SECRET="wrong",
                )
        assert resp.status_code == 401
        mock_call.assert_not_called()
