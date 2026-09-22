"""Torch field marketing stays off the retail sampling request type."""

import pytest

from events import models as em
from events.field_marketing import (
    FieldMarketingError,
    build_board,
    log_results,
    plan_event,
)
from events.tests.base import EventsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestFieldMarketing(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(
            name="Torch THC",
            slug="torch-thc",
            request_url_name="keee-torch-thc",
        )
        self.other = self.create_tenant(name="Other Brand", slug="other-brand")
        self.sys = self.get_system_user()
        self.marketer = self.create_user(
            username="alec",
            email="alec@torchdrinks.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.marketer, self.tenant)
        self.other_user = self.create_user(
            username="other",
            email="other@example.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.other_user, self.other)

    def _plan(self, **overrides):
        payload = {
            "market": "miami",
            "activity": "full_can",
            "name": "Wynwood drop",
            "starts_on": "2026-09-12",
            "days": 1,
            "address": "123 NW 2nd Ave, Miami, FL",
            "notes": "Guerilla",
            "planned_full_cans": 200,
            "planned_pour_samples": 0,
            "planned_emails": 40,
        }
        payload.update(overrides)
        return plan_event(user=self.marketer, payload=payload, submit=overrides.get("submit", False))

    def test_plan_does_not_create_a_request(self):
        event = self._plan()
        assert event.status == em.FieldMarketingEvent.STATUS_PLANNED
        assert event.request_id is None
        assert em.Request.objects.filter(tenant=self.tenant).count() == 0
        board = build_board(self.tenant, "2026-09")
        cans = next(row for row in board["kpis"] if row["key"] == "full_cans")
        assert cans["target"] == 1152
        assert cans["planned"] == 200
        assert cans["logged"] is None

    def test_submit_creates_field_marketing_request(self):
        event = self._plan(submit=True)
        event.refresh_from_db()
        assert event.status == em.FieldMarketingEvent.STATUS_SUBMITTED
        request = event.request
        assert request is not None
        assert request.request_type.name == "Field Marketing"
        assert request.status_id is None
        assert request.name.startswith("Field marketing ·")
        assert "Retail Sampling" not in request.request_type.name
        assert em.RequestActivityLog.objects.filter(request=request).exists()

    def test_pour_samples_do_not_count_as_full_cans(self):
        self._plan(
            activity="pour",
            name="Festival pour",
            planned_full_cans=999,
            planned_pour_samples=300,
        )
        board = build_board(self.tenant, "2026-09")
        cans = next(row for row in board["kpis"] if row["key"] == "full_cans")
        pours = next(row for row in board["kpis"] if row["key"] == "pour_samples")
        assert cans["planned"] == 0
        assert pours["planned"] == 300
        assert pours["target"] == 3456

    def test_logged_zero_is_a_result_and_a_blank_month_is_not(self):
        event = self._plan()
        board = build_board(self.tenant, "2026-09")
        emails = next(row for row in board["kpis"] if row["key"] == "emails")
        assert emails["logged"] is None
        log_results(
            user=self.marketer,
            event_id=str(event.uuid),
            payload={"logged_emails": 0, "logged_full_cans": 180},
        )
        board = build_board(self.tenant, "2026-09")
        emails = next(row for row in board["kpis"] if row["key"] == "emails")
        cans = next(row for row in board["kpis"] if row["key"] == "full_cans")
        assert emails["logged"] == 0
        assert cans["logged"] == 180

    def test_sponsorship_days_and_retail_count_separately(self):
        self._plan(
            activity="sponsorship",
            name="Austin fest",
            market="austin-dallas",
            days=3,
            planned_pour_samples=90,
        )
        retail = self._plan(
            activity="retail_support",
            name="DP meeting",
            market="houston",
            planned_emails=5,
        )
        board = build_board(self.tenant, "2026-09")
        days = next(row for row in board["kpis"] if row["key"] == "sponsorship_days")
        support = next(row for row in board["kpis"] if row["key"] == "retail_support")
        assert days["planned"] == 3
        assert days["target"] == 8
        assert days["logged"] is None
        assert support["planned"] == 1
        assert support["logged"] is None
        log_results(
            user=self.marketer,
            event_id=str(retail.uuid),
            payload={},
        )
        board = build_board(self.tenant, "2026-09")
        support = next(row for row in board["kpis"] if row["key"] == "retail_support")
        assert support["logged"] == 1

    def test_other_brand_cannot_plan(self):
        with pytest.raises(FieldMarketingError):
            plan_event(
                user=self.other_user,
                payload={
                    "market": "miami",
                    "activity": "full_can",
                    "name": "Nope",
                    "starts_on": "2026-09-12",
                },
                submit=False,
            )

    def test_submit_requires_an_address(self):
        with pytest.raises(FieldMarketingError):
            self._plan(address="", submit=True)
        assert em.Request.objects.filter(tenant=self.tenant).count() == 0
