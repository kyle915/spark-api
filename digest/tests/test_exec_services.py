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


class _ExecSummaryFixtures(AmbassadorsGraphQLTestCase):
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


@pytest.mark.django_db(transaction=True)
class TestExecutiveSummaryRecapFamilies(_ExecSummaryFixtures):

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


@pytest.mark.django_db(transaction=True)
class TestExecutiveSummaryEmailRenders(_ExecSummaryFixtures):
    """The template crashed at compile time on every Monday send
    (``summary.delta_chip:"recaps"`` is not valid Django template syntax).
    Render the real email from a realistic week -- legacy + custom-template
    recaps, a prior week so the delta chips show -- and assert it renders.
    """

    def _realistic_week(self):
        # Prior week: one custom recap, so both delta chips are populated.
        self._custom(days_ago=9, consumers=20)
        # This week: a custom-template recap pair plus a legacy recap, at
        # two stores.
        self._custom(consumers=40)
        self._custom(event=self._event("Store B"), consumers=25)
        self._legacy(consumers=10)
        return self._summary()

    def _render(self, summary, *, include_ba_names):
        from digest.envelopes import ExecutiveSummaryMailer

        mailer = ExecutiveSummaryMailer(
            summary=summary,
            to_emails=["someone@example.com"],
            web_app_base_url="https://admin.example.com",
            include_ba_names=include_ba_names,
        )
        return mailer.envelope().render_template()

    def test_the_email_renders_with_custom_template_recaps(self):
        summary = self._realistic_week()
        assert summary.recap_count == 3
        assert summary.recap_delta_chip and summary.reach_delta_chip

        html = self._render(summary, include_ba_names=True)

        assert summary.tenant_name in html
        assert summary.recap_delta_chip in html
        assert summary.reach_delta_chip in html
        assert "Top stores by reach" in html
        assert "samplings" in html
        assert "Dana Reyes" in html
        assert "Top ambassadors" in html

    def test_a_client_copy_carries_no_ambassador_names(self):
        summary = self._realistic_week()

        html = self._render(summary, include_ba_names=False)

        assert "Dana Reyes" not in html
        assert "ba-exec@test.com" not in html
        assert "Top ambassadors" not in html
        # The rest of the email is intact.
        assert "Top stores by reach" in html
        assert str(summary.recap_count) in html

    def test_no_cost_per_sample_in_the_email(self):
        html = self._render(self._realistic_week(), include_ba_names=True).lower()
        assert "cost per sample" not in html
        assert "cost-per-sample" not in html

    def test_cron_sends_names_to_staff_only(self):
        from unittest import mock

        from django.core.management import call_command

        from digest.envelopes import ExecutiveSummaryMailer
        from digest.management.commands.send_executive_summary import (
            split_staff_recipients,
        )

        client = self.create_user(
            username="client-exec@brand.com",
            email="client-exec@brand.com",
            role=self.roles["client"],
        )
        staff, others = split_staff_recipients(
            ["actor-exec@test.com", client.email, "stranger@nowhere.com"]
        )
        assert staff == ["actor-exec@test.com"]
        assert others == ["client-exec@brand.com", "stranger@nowhere.com"]

        self._realistic_week()
        rendered: list[tuple[list[str], str]] = []

        def fake_send(mailer):
            rendered.append((mailer.to_emails, mailer.envelope().render_template()))

        with mock.patch(
            "digest.management.commands.send_executive_summary.build_executive_summary",
            return_value=self._summary(),
        ), mock.patch.object(ExecutiveSummaryMailer, "send_now", fake_send):
            call_command(
                "send_executive_summary",
                "--tenant-id",
                str(self.tenant.id),
                "--to",
                f"actor-exec@test.com,{client.email}",
            )

        by_to = {tuple(to): html for to, html in rendered}
        assert "Dana Reyes" in by_to[("actor-exec@test.com",)]
        assert "Dana Reyes" not in by_to[(client.email,)]
