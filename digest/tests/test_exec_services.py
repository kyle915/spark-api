"""Weekly executive summary counts BOTH recap families.

A tenant on a custom recap template files ``CustomRecap`` rows and no legacy
``Recap`` rows. The summary read the legacy table only, so that tenant's week
came out as zero recaps and the email was skipped as empty.
"""

from datetime import datetime, timedelta, timezone as _tz

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from digest.exec_services import build_executive_summary
from events.models import Request, RequestType
from recaps.models import (
    ConsumerEngagements,
    CustomField,
    CustomFieldValue,
    CustomRecap,
    CustomRecapFieldType,
    CustomRecapTemplate,
    Recap,
    RecapSection,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=_tz.utc)


@pytest.mark.django_db(transaction=True)
class TestExecutiveSummaryRecapFamilies(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Custom Template Brand")
        self.actor = self.create_user(
            username="actor-exec@test.com",
            email="actor-exec@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-exec@test.com",
            email="ba-exec@test.com",
            role=self.roles["ambassador"],
            first_name="Dana",
            last_name="Reyes",
        )
        self.ba = self.create_ambassador(ba_user)
        self.etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Brand recap",
            event_type=self.etype,
            created_by=self.actor,
        )
        field_type, _ = CustomRecapFieldType.objects.get_or_create(
            name="number", defaults={"created_by": self.actor}
        )
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Numbers", order=0, created_by=self.actor
        )
        self.consumers_field = CustomField.objects.create(
            name="How many consumers sampled?",
            custom_recap_template=self.template,
            custom_field_type=field_type,
            recap_section=section,
            order=0,
            required=False,
            created_by=self.actor,
        )
        self.event = self._event("Store A")

    def _event(self, name, **kwargs):
        return self.create_event(
            name=name,
            tenant=self.tenant,
            address="1 Main St",
            event_type=self.etype,
            date=NOW - timedelta(days=2),
            **kwargs,
        )

    def _custom(self, *, event=None, days_ago=2, consumers=None, filed=True, **kwargs):
        recap = CustomRecap.objects.create(
            name="Recap",
            event=event or self.event,
            ambassador=self.ba,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.actor,
            updated_by=self.actor,
            submitted_at=(NOW - timedelta(days=days_ago)) if filed else None,
            **kwargs,
        )
        CustomRecap.objects.filter(id=recap.id).update(
            created_at=NOW - timedelta(days=days_ago)
        )
        if consumers is not None:
            CustomFieldValue.objects.create(
                custom_recap=recap,
                custom_field=self.consumers_field,
                value=str(consumers),
                created_by=self.actor,
            )
        return recap

    def _legacy(self, *, days_ago=2, consumers=None, filed=True):
        recap = Recap.objects.create(
            name="Legacy",
            event=self.event,
            ambassador=self.ba,
            created_by=self.actor,
            submited_at=(NOW - timedelta(days=days_ago)) if filed else None,
        )
        Recap.objects.filter(id=recap.id).update(
            created_at=NOW - timedelta(days=days_ago)
        )
        if consumers is not None:
            ConsumerEngagements.objects.create(
                recap=recap, created_by=self.actor, total_consumer=consumers
            )
        return recap

    def _summary(self):
        return build_executive_summary(self.tenant, now=NOW)

    def test_a_custom_template_tenant_is_not_reported_as_an_empty_week(self):
        self._custom(consumers=40)
        self._custom(consumers=25)

        summary = self._summary()

        assert summary.is_empty is False
        assert summary.recap_count == 2
        assert summary.consumer_reach == 65
        assert [(r.label, r.primary_metric) for r in summary.top_bas] == [
            ("Dana Reyes", 2)
        ]
        assert summary.top_stores[0].primary_metric == 65
        assert summary.top_stores[0].secondary_metric == "2 samplings"

    def test_legacy_and_custom_recaps_add_up(self):
        self._legacy(consumers=10)
        self._custom(consumers=40)

        summary = self._summary()

        assert summary.recap_count == 2
        assert summary.consumer_reach == 50
        assert summary.top_bas[0].primary_metric == 2

    def test_stubs_archived_excluded_and_deleted_requests_do_not_count(self):
        counted = self._custom(consumers=40)
        # Empty clock-out stubs: a row exists, nothing was filed.
        self._custom(filed=False)
        self._legacy(filed=False)
        # Parked by ops.
        self._custom(consumers=500, archived_at=NOW)
        # Agency-only filing kept out of aggregates.
        self._custom(consumers=500, exclude_from_aggregates=True)
        # Filed against a gig that was since deleted.
        request_type = RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.actor
        )
        deleted_request = Request.objects.create(
            name="Deleted gig",
            address="2 Main St",
            request_type=request_type,
            tenant=self.tenant,
            created_by=self.actor,
            date=NOW - timedelta(days=2),
            deleted_at=NOW,
        )
        self._custom(
            event=self._event("Deleted gig", request=deleted_request), consumers=500
        )
        # Outside the week.
        self._custom(days_ago=30, consumers=500)

        summary = self._summary()

        assert summary.recap_count == 1
        assert summary.consumer_reach == 40
        assert counted.id

    def test_week_over_week_uses_both_families(self):
        self._custom(days_ago=9, consumers=20)
        self._custom(consumers=30)
        self._custom(consumers=30)

        summary = self._summary()

        assert summary.recap_count_delta == 1
        assert summary.consumer_reach_delta == 40

    def test_a_template_with_no_consumer_question_counts_the_recap_only(self):
        self._custom()

        summary = self._summary()

        assert summary.recap_count == 1
        assert summary.consumer_reach == 0
