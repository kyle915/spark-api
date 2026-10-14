"""Recap metrics pushed to field-marketing plans: Planned vs Actual."""

import csv
import io
import json
from datetime import datetime
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.core.management import call_command
from django.utils import timezone

from events import models as em
from events.field_marketing import build_board, log_results, plan_event
from events.mutations import _materialize_approved_event_for_request
from events.plan_event_types import (
    event_type_fits_plan,
    event_type_for_plan,
    event_type_for_request,
)
from events.plan_results import (
    PlanResultsError,
    auto_attach_on_approval,
    build_plan_results,
    push_recap,
    remove_recap,
    suggest_plans,
    suggest_recaps,
)
from events.demo_cancel import request_display_code
from events.tests.base import EventsGraphQLTestCase
from recaps import models as rm
from recaps import plan_metrics as pm

EVENT_TEMPLATE = "Torch THC-Event Activation"
SEEDING_TEMPLATE = "Torch THC · Product Seeding Recap"
SKUS = ("Black Cherry 10mg", "Strawberry Lemonade 10mg", "Watermelon Limeade 10mg", "Nonactive")

PICKER = """
query Picker($recapId: ID!, $kind: String!) {
  recapPlanPicker(recapId: $recapId, kind: $kind) {
    available executionType currentPlan { id name }
    metrics { key label value text defaultOn parts { label value } }
    suggestions { plan { id name } reasons linked }
  }
}
"""

PUSH = """
mutation Push($input: PushRecapToPlanInput!) {
  pushRecapToPlan(input: $input) { success message metricKeys plan { id name } }
}
"""

RESULTS = """
query Results($planId: String!) {
  fieldMarketingPlanResults(planId: $planId) {
    counted pending
    rows { key label planned actual unit text note parts { label value } }
    recaps { uuid status auto href metrics { key text } }
  }
}
"""

APPROVE = """
mutation Approve($input: ApproveCustomRecapInput!) {
  approveCustomRecap(input: $input) { success }
}
"""


@pytest.mark.django_db(transaction=True)
class TestPlanResults(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.other = self.create_tenant(name="Other Brand", slug="other-brand")
        self.admin = self.create_user(
            username="ops",
            email="ops@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.marketer = self.create_user(
            username="alec", email="alec@torchdrinks.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.marketer, self.tenant)
        self.outsider = self.create_user(
            username="nope", email="nope@example.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.outsider, self.other)
        ba_user = self.create_user(
            username="ba1", email="ba1@example.com", role=self.roles["ambassador"],
            first_name="Jamie", last_name="Cruz",
        )
        self.ba = self.create_ambassador(ba_user)
        em.RequestType.objects.create(
            tenant=self.tenant, name="Event Activation", created_by=self.sys
        )
        line = em.ProductType.objects.create(
            tenant=self.tenant, name="Field marketing", created_by=self.sys
        )
        for name in SKUS:
            em.Product.objects.create(
                tenant=self.tenant, product_type=line, name=name, created_by=self.sys
            )
        self.number = rm.CustomRecapFieldType.objects.create(name="number", created_by=self.sys)
        self.section = rm.RecapSection.objects.create(
            name="Consumer Engagement", tenant=self.tenant, created_by=self.sys
        )
        self.event_type = em.EventType.objects.create(
            name="Event Activation", slug="ea-torch", tenant=self.tenant, created_by=self.sys
        )
        self.template = rm.CustomRecapTemplate.objects.create(
            name=EVENT_TEMPLATE,
            event_type=self.event_type,
            tenant=self.tenant,
            layout={"sampleQtyLabel": "Cans sampled", "sampleQtyTotalLabel": "Total cans sampled"},
            created_by=self.sys,
        )
        self.seeding_type = em.EventType.objects.create(
            name="Product Seeding", slug="ps-torch", tenant=self.tenant, created_by=self.sys
        )
        self.seeding_template = rm.CustomRecapTemplate.objects.create(
            name=SEEDING_TEMPLATE,
            event_type=self.seeding_type,
            tenant=self.tenant,
            created_by=self.sys,
        )

    # -- helpers ------------------------------------------------------------

    def _plan(self, submit=True, **overrides):
        payload = {
            "market": "miami",
            "activity": "event_activation",
            "name": "Wynwood fest",
            "starts_on": "2026-09-12",
            "days": 1,
            "address": "250 NW 24th St, Miami, FL 33127",
            "sampling_format": "full_can",
            "sku_names": ["Black Cherry 10mg", "Nonactive"],
            "needs_field_support": True,
            "ambassador_count": 2,
            "support_times": "4–8pm",
            "planned_emails": 150,
        }
        payload.update(overrides)
        return plan_event(
            user=self.marketer, payload=payload, submit=submit, tenant_id=self.tenant.id
        )

    def _event(self, *, request=None, day="2026-09-12", address=None, event_type=None):
        when = timezone.make_aware(datetime.fromisoformat(f"{day}T15:00:00"))
        return em.Event.objects.create(
            name="Event",
            tenant=self.tenant,
            request=request,
            date=when,
            start_time=when,
            address=address or "250 NW 24th St, Miami, FL 33127",
            event_type=event_type or self.event_type,
            created_by=self.sys,
        )

    def _field(self, recap, name, value, template=None):
        field = rm.CustomField.objects.filter(
            custom_recap_template=template or self.template, name=name
        ).first() or rm.CustomField.objects.create(
            name=name,
            custom_recap_template=template or self.template,
            custom_field_type=self.number,
            recap_section=self.section,
            created_by=self.sys,
        )
        rm.CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=field, value=value, created_by=self.sys
        )

    def _event_recap(self, event, *, approved=True, emails="110", sampled="85"):
        recap = rm.CustomRecap.objects.create(
            name="Wynwood recap",
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.template,
            ambassador=self.ba,
            approved=approved,
            created_by=self.sys,
        )
        self._field(recap, "How many TOTAL consumers did you sample?", sampled)
        self._field(recap, "People engaged", "140")
        self._field(recap, "Email addresses collected", emails)
        self._field(recap, "Collection method", json.dumps(["QR code"]))
        self._field(recap, "Sample format", json.dumps(["Full can"]))
        for name, qty in (("Black Cherry 10mg", 40), ("Watermelon Limeade 10mg", 24)):
            rm.CustomRecapProductSample.objects.create(
                custom_recap=recap,
                product=em.Product.objects.get(tenant=self.tenant, name=name),
                quantity=qty,
            )
        return recap

    def _keys(self, recap):
        return [m.key for m in pm.recap_metrics(recap, pm.KIND_CUSTOM)]

    # -- metrics ------------------------------------------------------------

    def test_metrics_read_the_recap_without_conflating(self):
        recap = self._event_recap(self._event())
        metrics = {m.key: m for m in pm.recap_metrics(recap, pm.KIND_CUSTOM)}
        assert metrics[pm.CONSUMERS_SAMPLED].value == 85
        assert metrics[pm.PEOPLE_ENGAGED].value == 140
        assert metrics[pm.EMAILS_COLLECTED].value == 110
        assert metrics[pm.EMAILS_COLLECTED].text == "110 · QR code"
        samples = metrics[pm.SAMPLES_BY_SKU]
        assert samples.label == "Total cans sampled"
        assert samples.value == 64
        assert dict(samples.parts) == {"Black Cherry 10mg": 40, "Watermelon Limeade 10mg": 24}
        assert metrics[pm.SAMPLE_FORMAT].text == "Full can"
        # Event recaps never report sales.
        assert pm.UNITS_SOLD not in metrics
        assert pm.default_keys(recap, list(metrics.values())) == [
            pm.CONSUMERS_SAMPLED,
            pm.PEOPLE_ENGAGED,
            pm.SAMPLES_BY_SKU,
            pm.EMAILS_COLLECTED,
            pm.SAMPLE_FORMAT,
        ]

    def test_seeding_metrics_cases_locations_mileage(self):
        event = self._event(event_type=self.seeding_type)
        recap = rm.CustomRecap.objects.create(
            name="Seeding",
            event=event,
            tenant=self.tenant,
            custom_recap_template=self.seeding_template,
            approved=True,
            created_by=self.sys,
        )
        stops = [
            {"placeName": "Bar A", "skus": [{"productId": "1", "productName": "Nonactive", "cases": 2}]},
            {"placeName": "Bar B", "skus": [{"productId": "2", "productName": "Black Cherry 10mg", "cases": 3}]},
        ]
        self._field(recap, "Drop-off Locations", json.dumps(stops), self.seeding_template)
        self._field(recap, "Total mileage", "42.5", self.seeding_template)
        metrics = {m.key: m for m in pm.recap_metrics(recap, pm.KIND_CUSTOM)}
        assert metrics[pm.CASES_DROPPED].value == 5
        assert metrics[pm.DROP_OFF_LOCATIONS].value == 2
        assert metrics[pm.MILEAGE].value == 42.5
        assert pm.CONSUMERS_SAMPLED not in metrics

    # -- suggestions --------------------------------------------------------

    def test_suggestions_rank_booked_plan_then_same_tactic_market(self):
        booked = self._plan()
        nearby = self._plan(name="Brickell pop-up", starts_on="2026-09-14")
        other_tactic = self._plan(
            submit=False, name="Seeding run", activity="product_seeding", starts_on="2026-09-12"
        )
        self._plan(submit=False, name="Far away", starts_on="2026-10-30")
        houston = self._plan(
            submit=False, name="Houston fest", market="houston", starts_on="2026-09-12",
            address="1 Main St, Houston, TX",
        )
        recap = self._event_recap(self._event(request=booked.request))
        ranked = suggest_plans(recap, pm.KIND_CUSTOM)
        names = [s.plan.name for s in ranked]
        assert names[0] == booked.name
        assert ranked[0].linked and "Booked from this plan" in ranked[0].reasons
        assert names[1] == nearby.name
        assert "Far away" not in names
        assert names.index(other_tactic.name) > names.index(nearby.name)
        assert names.index(houston.name) > names.index(nearby.name)
        assert [s.plan.name for s in suggest_plans(recap, pm.KIND_CUSTOM, "far")] == ["Far away"]

    # -- push / move / remove ----------------------------------------------

    def test_push_validates_and_move_needs_confirm(self):
        plan = self._plan()
        second = self._plan(name="Second", starts_on="2026-09-13")
        recap = self._event_recap(self._event(request=plan.request))
        with pytest.raises(PlanResultsError, match="at least one"):
            push_recap(kind="custom", recap_id=recap.id, plan_id=str(plan.uuid),
                       metric_keys=[], actor=self.admin, confirm_move=False)
        with pytest.raises(PlanResultsError, match="didn't answer"):
            push_recap(kind="custom", recap_id=recap.id, plan_id=str(plan.uuid),
                       metric_keys=[pm.UNITS_SOLD], actor=self.admin, confirm_move=False)
        link = push_recap(kind="custom", recap_id=recap.id, plan_id=str(plan.uuid),
                          metric_keys=[pm.EMAILS_COLLECTED, pm.CONSUMERS_SAMPLED],
                          actor=self.admin, confirm_move=False)
        assert link.metric_keys == [pm.CONSUMERS_SAMPLED, pm.EMAILS_COLLECTED]
        with pytest.raises(PlanResultsError, match="Confirm to move"):
            push_recap(kind="custom", recap_id=recap.id, plan_id=str(second.uuid),
                       metric_keys=[pm.EMAILS_COLLECTED], actor=self.admin, confirm_move=False)
        moved = push_recap(kind="custom", recap_id=recap.id, plan_id=str(second.uuid),
                           metric_keys=[pm.EMAILS_COLLECTED], actor=self.admin, confirm_move=True)
        assert moved.id == link.id and moved.plan_id == second.id
        assert rm.PlanRecapLink.objects.count() == 1
        assert remove_recap(kind="custom", recap_id=recap.id) is True
        assert rm.PlanRecapLink.objects.count() == 0

    def test_deleted_plans_are_not_suggested_or_pushable(self):
        plan = self._plan()
        plan.deleted_at = timezone.now()
        plan.save(update_fields=["deleted_at"])
        recap = self._event_recap(self._event(request=plan.request))
        assert plan.name not in [s.plan.name for s in suggest_plans(recap, pm.KIND_CUSTOM)]
        with pytest.raises(PlanResultsError, match="Pick a plan"):
            push_recap(kind="custom", recap_id=recap.id, plan_id=str(plan.uuid),
                       metric_keys=[pm.EMAILS_COLLECTED], actor=self.admin, confirm_move=False)
        assert auto_attach_on_approval(kind="custom", recap_id=recap.id, actor=self.admin) is None

    def test_push_rejects_another_brands_plan(self):
        plan = self._plan()
        other_event = em.Event.objects.create(
            name="x", tenant=self.other, address="1 Main", created_by=self.sys
        )
        other_type = em.EventType.objects.create(
            name="Event Activation", slug="ea-other", tenant=self.other, created_by=self.sys
        )
        other_template = rm.CustomRecapTemplate.objects.create(
            name="Other", event_type=other_type, tenant=self.other, created_by=self.sys
        )
        recap = rm.CustomRecap.objects.create(
            name="Other recap", event=other_event, tenant=self.other,
            custom_recap_template=other_template, created_by=self.sys,
        )
        with pytest.raises(PlanResultsError, match="another brand"):
            push_recap(kind="custom", recap_id=recap.id, plan_id=str(plan.uuid),
                       metric_keys=[], actor=self.admin, confirm_move=False)

    # -- results ------------------------------------------------------------

    def test_results_sum_approved_only_and_compare_planned(self):
        plan = self._plan()
        event = self._event(request=plan.request)
        first = self._event_recap(event, emails="110", sampled="85")
        second = self._event_recap(event, emails="60", sampled="40")
        pending = self._event_recap(event, approved=False, emails="999", sampled="999")
        keys = [pm.CONSUMERS_SAMPLED, pm.SAMPLES_BY_SKU, pm.EMAILS_COLLECTED]
        for recap in (first, second, pending):
            push_recap(kind="custom", recap_id=recap.id, plan_id=str(plan.uuid),
                       metric_keys=keys, actor=self.admin, confirm_move=False)

        admin_view = build_plan_results(plan, is_admin=True)
        rows = {row.key: row for row in admin_view["rows"]}
        assert admin_view["counted"] == 2 and admin_view["pending"] == 1
        assert rows[pm.EMAILS_COLLECTED].planned == 150
        assert rows[pm.EMAILS_COLLECTED].actual == 170
        assert rows[pm.CONSUMERS_SAMPLED].actual == 125
        assert rows[pm.SAMPLES_BY_SKU].actual == 128
        assert rows[pm.SAMPLES_BY_SKU].label == "Total cans sampled"
        assert "not on recaps: Nonactive" in rows[pm.SAMPLES_BY_SKU].note
        assert rows["brand_ambassadors"].planned == 2
        assert rows["brand_ambassadors"].actual == 1
        # People engaged wasn't ticked, so it doesn't show up as a result.
        assert pm.PEOPLE_ENGAGED not in rows
        statuses = sorted(r["status"] for r in admin_view["recaps"])
        assert statuses == ["approved", "approved", "needs_review"]

        client_view = build_plan_results(plan, is_admin=False)
        assert client_view["pending"] == 0
        assert [r["status"] for r in client_view["recaps"]] == ["approved", "approved"]
        assert {row.key: row for row in client_view["rows"]}[pm.EMAILS_COLLECTED].actual == 170

    def test_board_shows_results_and_recap_logged_fallback(self):
        plan = self._plan()
        recap = self._event_recap(self._event(request=plan.request))
        push_recap(kind="custom", recap_id=recap.id, plan_id=str(plan.uuid),
                   metric_keys=[pm.EMAILS_COLLECTED, pm.SAMPLES_BY_SKU],
                   actor=self.admin, confirm_move=False)
        pending = self._event_recap(self._event(request=plan.request), approved=False)
        push_recap(kind="custom", recap_id=pending.id, plan_id=str(plan.uuid),
                   metric_keys=[pm.EMAILS_COLLECTED], actor=self.admin, confirm_move=False)

        board = build_board(self.tenant, "2026-09", include_pending=True)
        row = next(r for r in board["events"] if r["id"] == str(plan.uuid))
        assert row["results_counted"] == 1 and row["results_pending"] == 1
        kpis = {k["key"]: k for k in board["kpis"]}
        assert kpis["emails"]["logged"] == 110
        assert kpis["full_cans"]["logged"] == 64
        client_board = build_board(self.tenant, "2026-09")
        assert next(r for r in client_board["events"] if r["id"] == str(plan.uuid))["results_pending"] == 0

        # A manual log still wins over recap actuals.
        log_results(user=self.marketer, event_id=str(plan.uuid),
                    payload={"logged_emails": 90}, tenant_id=self.tenant.id)
        board = build_board(self.tenant, "2026-09")
        assert {k["key"]: k for k in board["kpis"]}["emails"]["logged"] == 90

    def test_pour_plan_never_counts_cans_as_full_cans(self):
        plan = self._plan(sampling_format="pour")
        recap = self._event_recap(self._event(request=plan.request))
        push_recap(kind="custom", recap_id=recap.id, plan_id=str(plan.uuid),
                   metric_keys=[pm.SAMPLES_BY_SKU], actor=self.admin, confirm_move=False)
        kpis = {k["key"]: k for k in build_board(self.tenant, "2026-09")["kpis"]}
        assert kpis["full_cans"]["logged"] is None

    # -- auto attach --------------------------------------------------------

    def test_auto_attach_uses_booked_request_and_keeps_existing_link(self):
        plan = self._plan()
        recap = self._event_recap(self._event(request=plan.request))
        link = auto_attach_on_approval(kind="custom", recap_id=recap.id, actor=self.admin)
        assert link is not None and link.auto and link.plan_id == plan.id
        assert pm.EMAILS_COLLECTED in link.metric_keys
        assert auto_attach_on_approval(kind="custom", recap_id=recap.id, actor=self.admin) is None

        walkup = self._event_recap(self._event())
        assert auto_attach_on_approval(kind="custom", recap_id=walkup.id, actor=self.admin) is None

    def test_third_party_recaps_stay_out_of_plan_actuals(self):
        plan = self._plan()
        event = self._event(request=plan.request)
        agency = self._event_recap(event, emails="500", sampled="500")
        agency.is_third_party = agency.exclude_from_aggregates = True
        agency.save(update_fields=["is_third_party", "exclude_from_aggregates"])
        assert auto_attach_on_approval(kind="custom", recap_id=agency.id, actor=self.admin) is None

        ours = self._event_recap(event)
        for recap in (agency, ours):
            push_recap(kind="custom", recap_id=recap.id, plan_id=str(plan.uuid),
                       metric_keys=[pm.EMAILS_COLLECTED], actor=self.admin, confirm_move=False)
        view = build_plan_results(plan, is_admin=True)
        assert view["counted"] == 1
        assert {row.key: row for row in view["rows"]}[pm.EMAILS_COLLECTED].actual == 110
        assert sorted(r["status"] for r in view["recaps"]) == ["approved", "third_party"]
        board = build_board(self.tenant, "2026-09", include_pending=True)
        row = next(r for r in board["events"] if r["id"] == str(plan.uuid))
        assert (row["results_counted"], row["results_pending"]) == (1, 0)
        assert {k["key"]: k for k in board["kpis"]}["emails"]["logged"] == 110

    # -- audit ----------------------------------------------------------------

    def _audit(self, *args) -> str:
        out = io.StringIO()
        call_command("audit_torch_plan_buckets", *args, stdout=out)
        return out.getvalue()

    def _csv_rows(self, log: str, section: str) -> list[dict]:
        body = log.split(f"----- {section} CSV BEGIN -----\n", 1)[1]
        body = body.split(f"\n----- {section} CSV END -----", 1)[0]
        return list(csv.DictReader(io.StringIO(body)))

    def test_audit_flags_missing_and_wrong_tactic_and_attaches_on_apply(self):
        plan = self._plan()
        missing = self._event_recap(self._event(request=plan.request))
        seeding_plan = self._plan(
            submit=False, name="Seeding run", activity="product_seeding", starts_on="2026-09-12"
        )
        misfiled = self._event_recap(self._event())
        push_recap(kind="custom", recap_id=misfiled.id, plan_id=str(seeding_plan.uuid),
                   metric_keys=[pm.EMAILS_COLLECTED], actor=self.admin, confirm_move=False)

        log = self._audit("--attach-missing")
        rows = {int(r["recap_id"]): r for r in self._csv_rows(log, "RECAPS")}
        assert rows[missing.id]["flags"] == "MISSING"
        assert "WRONG_TACTIC" in rows[misfiled.id]["flags"].split()
        assert rows[misfiled.id]["contributed"] == "emails_collected=110"
        assert "WOULD ATTACH" in log
        assert not rm.PlanRecapLink.objects.filter(custom_recap=missing).exists()
        plans = {r["plan_name"]: r for r in self._csv_rows(log, "PLANS")}
        assert plans["Seeding run"]["actual_rows"] == "emails_collected=110"
        assert plans["Seeding run"]["flags"] == ""

        log = self._audit("--attach-missing", "--apply")
        link = rm.PlanRecapLink.objects.get(custom_recap=missing)
        assert link.plan_id == plan.id and link.auto
        assert pm.SAMPLES_BY_SKU in link.metric_keys
        rows = {int(r["recap_id"]): r for r in self._csv_rows(self._audit(), "RECAPS")}
        assert rows[missing.id]["flags"] == ""
        assert rows[missing.id]["counted"] == "yes"

    # -- plan event program -------------------------------------------------

    def _torch_programs(self):
        def make(name, slug, **extra):
            return em.EventType.objects.create(
                name=name, slug=slug, tenant=self.tenant, created_by=self.sys, **extra
            )

        return {
            "retail": make("Retail Sampling", "rs-torch", is_default=True),
            "event": make("Event", "ev-torch"),
            "guerilla": make("Guerilla Activation", "ga-torch"),
        }

    def test_plan_books_on_its_tactics_program(self):
        programs = self._torch_programs()
        event_plan = self._plan()
        guerilla_plan = self._plan(
            submit=False, activity="guerilla", name="Pop-up", needs_field_support=True
        )
        seeding_plan = self._plan(submit=False, activity="product_seeding", name="Seed")
        sales_plan = self._plan(
            submit=False, activity="sales_support", name="Visit", support_type="retail_visit"
        )
        assert event_type_for_plan(event_plan) == self.event_type
        assert event_type_for_plan(guerilla_plan) == programs["guerilla"]
        assert event_type_for_plan(seeding_plan) == self.seeding_type
        assert event_type_for_plan(sales_plan) is None
        assert not event_type_fits_plan(programs["guerilla"], event_plan)
        assert not event_type_fits_plan(programs["retail"], event_plan)
        assert event_type_fits_plan(programs["event"], event_plan)
        assert event_type_for_request(event_plan.request) == self.event_type

        em.EventStatus.objects.create(
            name="Approved", slug="approved", tenant=self.tenant, created_by=self.sys
        )
        event = async_to_sync(_materialize_approved_event_for_request)(
            event_plan.request, self.admin
        )
        assert event.event_type_id == self.event_type.id

    def test_audit_sets_plan_event_program_only_before_recaps(self):
        programs = self._torch_programs()
        plan = self._plan()
        empty = self._event(request=plan.request, event_type=programs["retail"])
        filed = self._event(request=plan.request, event_type=programs["retail"])
        self._event_recap(filed)

        log = self._audit("--fix-event-types")
        assert f"event={empty.id} program='Retail Sampling'" in log
        assert "WOULD SET 'Event Activation'" in log
        assert "LEAVE (recaps already filed)" in log
        empty.refresh_from_db()
        assert empty.event_type_id == programs["retail"].id

        self._audit("--fix-event-types", "--apply")
        empty.refresh_from_db()
        filed.refresh_from_db()
        assert empty.event_type_id == self.event_type.id
        assert filed.event_type_id == programs["retail"].id

    # -- GraphQL ------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_graphql_picker_push_and_client_results(self):
        plan = await sync_to_async(self._plan)()
        event = await sync_to_async(self._event)(request=plan.request)
        recap = await sync_to_async(self._event_recap)(event)

        denied = await self._execute_mutation(
            PICKER, {"recapId": str(recap.id), "kind": "custom"}, user=self.marketer
        )
        assert denied.errors

        picker = await self._execute_mutation(
            PICKER, {"recapId": str(recap.id), "kind": "custom"}, user=self.admin
        )
        assert picker.errors is None, picker.errors
        data = picker.data["recapPlanPicker"]
        assert data["available"] is True and data["executionType"] == "event"
        assert data["suggestions"][0]["linked"] is True
        on = [m["key"] for m in data["metrics"] if m["defaultOn"]]
        assert pm.EMAILS_COLLECTED in on

        pushed = await self._execute_mutation(
            PUSH,
            {"input": {"recapId": str(recap.id), "kind": "custom",
                       "planId": str(plan.uuid), "metricKeys": on}},
            user=self.admin,
        )
        assert pushed.errors is None, pushed.errors
        assert pushed.data["pushRecapToPlan"]["plan"]["name"] == plan.name

        mine = await self._execute_mutation(
            RESULTS, {"planId": str(plan.uuid)}, user=self.marketer
        )
        assert mine.errors is None, mine.errors
        results = mine.data["fieldMarketingPlanResults"]
        assert results["counted"] == 1
        assert results["recaps"][0]["href"] == f"/recap/view-custom/{recap.uuid}"

        theirs = await self._execute_mutation(
            RESULTS, {"planId": str(plan.uuid)}, user=self.outsider
        )
        assert theirs.errors

    @pytest.mark.asyncio
    async def test_approve_mutation_auto_attaches_without_extra_mail(self):
        plan = await sync_to_async(self._plan)()
        event = await sync_to_async(self._event)(request=plan.request)
        recap = await sync_to_async(self._event_recap)(event, approved=False)
        with patch("recaps.mutations.RecapApprovedNotificationMailer.send"):
            result = await self._execute_mutation(
                APPROVE, {"input": {"id": str(recap.id), "approved": True}}, user=self.admin
            )
        assert result.errors is None, result.errors
        link = await sync_to_async(
            lambda: rm.PlanRecapLink.objects.filter(custom_recap=recap).first()
        )()
        assert link is not None and link.auto and link.plan_id == plan.id

    # -- attach from the plan side -------------------------------------------

    def test_plan_side_suggestions_skip_third_party_and_other_tactics(self):
        plan = self._plan(name="Dania Beach Food & Wine", sampling_format="pour")
        walkup = self._event_recap(self._event(address="1 Ocean Dr, Dania Beach, FL 33004"))
        booked = self._event_recap(self._event(request=plan.request))
        agency = self._event_recap(self._event())
        agency.is_third_party = agency.exclude_from_aggregates = True
        agency.save(update_fields=["is_third_party", "exclude_from_aggregates"])
        seeding_event = self._event(event_type=self.seeding_type)
        seeding = rm.CustomRecap.objects.create(
            name="Seeding", event=seeding_event, tenant=self.tenant,
            custom_recap_template=self.seeding_template, approved=True, created_by=self.sys,
        )
        far = self._event_recap(self._event(day="2026-10-30"))

        ids = [s.recap.id for s in suggest_recaps(plan)]
        assert ids[0] == booked.id
        assert walkup.id in ids
        assert agency.id not in ids and seeding.id not in ids and far.id not in ids

        push_recap(kind="custom", recap_id=walkup.id, plan_id=str(plan.uuid),
                   metric_keys=[pm.EMAILS_COLLECTED], actor=self.admin, confirm_move=False)
        assert walkup.id not in [s.recap.id for s in suggest_recaps(plan)]

    @pytest.mark.asyncio
    async def test_graphql_plan_recap_suggestions_admin_only(self):
        plan = await sync_to_async(self._plan)()
        recap = await sync_to_async(self._event_recap)(await sync_to_async(self._event)())
        query = """
        query S($planId: String!) {
          planRecapSuggestions(planId: $planId) {
            recapId kind href reasons currentPlanName metrics { key defaultOn }
          }
        }
        """
        denied = await self._execute_mutation(query, {"planId": str(plan.uuid)}, user=self.marketer)
        assert denied.errors
        ok = await self._execute_mutation(query, {"planId": str(plan.uuid)}, user=self.admin)
        assert ok.errors is None, ok.errors
        row = ok.data["planRecapSuggestions"][0]
        assert row["recapId"] == str(recap.id) and row["currentPlanName"] is None
        assert "Same tactic (Event)" in row["reasons"]
        assert pm.EMAILS_COLLECTED in [m["key"] for m in row["metrics"] if m["defaultOn"]]

    def test_audit_attach_dry_run_rolls_back_and_apply_links(self):
        plan = self._plan(name="Dania Beach Food & Wine")
        code = request_display_code(plan.request_id)
        first = self._event_recap(self._event(address="Frost Park, Dania Beach, FL 33004"))
        second = self._event_recap(self._event(address="Frost Park, Dania Beach, FL 33004"))
        unapproved = self._event_recap(self._event(), approved=False)
        specs = (
            f"custom:{first.id}={code}; custom:{second.id}={code}:clocked_hours+photos+people_engaged;"
            f" custom:{unapproved.id}={code}"
        )

        log = self._audit("--attach", specs)
        assert "address='Frost Park, Dania Beach, FL 33004'" in log
        assert "Consumers sampled: planned — · actual 85" in log
        assert "WOULD ATTACH (dry run, rolled back)" in log
        assert "LEAVE: not approved" in log
        assert not rm.PlanRecapLink.objects.exists()

        self._audit("--attach", specs, "--apply")
        links = {link.custom_recap_id: link for link in rm.PlanRecapLink.objects.all()}
        assert set(links) == {first.id, second.id}
        assert not links[first.id].auto and pm.CONSUMERS_SAMPLED in links[first.id].metric_keys
        assert links[second.id].metric_keys == [pm.PEOPLE_ENGAGED]
        rows = {r.key: r for r in build_plan_results(plan, is_admin=True)["rows"]}
        assert rows[pm.CONSUMERS_SAMPLED].actual == 85
        assert rows[pm.PEOPLE_ENGAGED].actual == 280
        assert rows["brand_ambassadors"].actual == 1

    def test_seeding_recap_on_plan_event_attaches_cases_locations_mileage(self):
        plan = self._plan(submit=False, activity="product_seeding", name="Bar drops")
        plan.request = em.Request.objects.create(
            name="Bar drops", tenant=self.tenant, created_by=self.sys,
            request_type=em.RequestType.objects.get(tenant=self.tenant),
        )
        plan.save(update_fields=["request"])
        event = self._event(request=plan.request, event_type=self.seeding_type)
        recap = rm.CustomRecap.objects.create(
            name="Seeding", event=event, tenant=self.tenant,
            custom_recap_template=self.seeding_template, approved=True, created_by=self.sys,
        )
        stops = [{"placeName": "Bar A", "skus": [{"productId": "1", "productName": "Nonactive", "cases": 2}]}]
        self._field(recap, "Drop-off Locations", json.dumps(stops), self.seeding_template)
        self._field(recap, "Total mileage", "12", self.seeding_template)
        link = auto_attach_on_approval(kind="custom", recap_id=recap.id, actor=self.admin)
        assert link.metric_keys == [pm.CASES_DROPPED, pm.DROP_OFF_LOCATIONS, pm.MILEAGE]
        kpis = {k["key"]: k for k in build_board(self.tenant, "2026-09")["kpis"]}
        assert kpis["full_cans"]["logged"] is None

    def test_unknown_kind_and_missing_plan(self):
        with pytest.raises(PlanResultsError):
            push_recap(kind="nope", recap_id=1, plan_id="x", metric_keys=[],
                       actor=self.admin, confirm_move=False)
        recap = self._event_recap(self._event())
        with pytest.raises(PlanResultsError, match="Pick a plan"):
            push_recap(kind="custom", recap_id=recap.id, plan_id="",
                       metric_keys=[pm.EMAILS_COLLECTED], actor=self.admin, confirm_move=False)
