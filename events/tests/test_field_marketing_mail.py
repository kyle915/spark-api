"""Plan-submitted emails: submitter confirmation + Torch internal list."""

import re
from datetime import date
from unittest.mock import patch

import pytest

from events import models as em
from events.envelopes import ClientRequestCreatedNotificationMailer
from events.field_marketing import (
    FIELD_MARKETING_SKU_NAMES,
    _summary_rows,
    plan_event,
    submit_event,
)
from events.field_marketing_mail import (
    TORCH_PLAN_SUBMIT_INTERNAL_EMAILS,
    PlanSubmittedConfirmationMailer,
    PlanSubmittedInternalMailer,
    notify_plan_submitted,
    plan_submit_mail_for,
)
from events.tests.base import EventsGraphQLTestCase
from utils.mailer import Mailer

INTERNAL = set(TORCH_PLAN_SUBMIT_INTERNAL_EMAILS)


@pytest.fixture
def sent():
    """Every envelope a Mailer tried to send, in order."""
    envelopes = []

    def _capture(self, delay_seconds=None):
        envelopes.append((type(self), self.envelope()))

    with patch.object(Mailer, "send", autospec=True, side_effect=_capture):
        yield envelopes


def _of(sent, mailer_cls):
    return [envelope for cls, envelope in sent if cls is mailer_cls]


@pytest.mark.django_db(transaction=True)
class TestPlanSubmittedMail(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(
            name="Torch THC", slug="torch-thc", request_url_name="keee-torch-thc"
        )
        self.other = self.create_tenant(name="Other Brand", slug="other-brand")
        self.marketer = self.create_user(
            username="alec",
            email="Alec@TorchDrinks.com",
            role=self.roles["client"],
            first_name="Alec",
            last_name="Aparicio",
        )
        self.create_tenanted_user(self.marketer, self.tenant)
        self.ops = self.create_user(
            username="ops",
            email="ops@igniteproductions.co",
            role=self.roles["spark_admin"],
        )
        self.kyle = self.create_user(
            username="kyle",
            email="kyle@igniteproductions.co",
            role=self.roles["spark_admin"],
            first_name="Kyle",
            last_name="Christiansen",
        )
        self.create_tenanted_user(self.kyle, self.tenant)
        line = em.ProductType.objects.create(
            tenant=self.tenant, name="Field marketing", created_by=self.sys
        )
        for name in FIELD_MARKETING_SKU_NAMES:
            em.Product.objects.create(
                tenant=self.tenant, product_type=line, name=name, created_by=self.sys
            )
        em.RequestType.objects.create(
            tenant=self.tenant, name="Event Activation", created_by=self.sys
        )

    def _seeding(self, *, user=None, submit=True):
        return plan_event(
            user=user or self.marketer,
            payload={
                "market": "miami",
                "activity": "product_seeding",
                "name": "Wynwood case drop",
                "starts_on": "2026-10-17",
                "sku_names": ["Black Cherry 10mg"],
                "planned_emails": 40,
            },
            submit=submit,
            tenant_id=self.tenant.id,
        )

    def _activation(self, *, submit=True):
        return plan_event(
            user=self.marketer,
            payload={
                "market": "houston",
                "activity": "event_activation",
                "name": "Montrose block party",
                "starts_on": "2026-10-24",
                "days": 2,
                "address": "1001 Westheimer Rd, Houston, TX 77006",
                "sampling_format": "pour",
                "sku_names": ["Black Cherry 10mg", "Nonactive"],
                "needs_field_support": True,
                "ambassador_count": 2,
                "support_times": "2pm load-in",
                "support_scope": "Sampling tent",
            },
            submit=submit,
        )

    def test_submitter_gets_confirmation_and_internal_list_gets_notified(self, sent):
        self._seeding()

        [confirm] = _of(sent, PlanSubmittedConfirmationMailer)
        assert [e.lower() for e in confirm.to_emails] == ["alec@torchdrinks.com"]
        assert confirm.subject == "Torch THC: your field marketing plan was submitted"

        [internal] = _of(sent, PlanSubmittedInternalMailer)
        assert internal.to_emails == list(TORCH_PLAN_SUBMIT_INTERNAL_EMAILS)
        assert internal.subject == (
            "New Torch THC field marketing plan: Wynwood case drop "
            "(submitted by Alec Aparicio)"
        )
        assert internal.headers["Reply-To"].lower() == "alec@torchdrinks.com"
        # Plan-only tactic: no request, so no generic new-request alert.
        assert _of(sent, ClientRequestCreatedNotificationMailer) == []

    def test_internal_list_is_exact_and_lowercase(self):
        assert INTERNAL == {
            "events@igniteproductions.co",
            "kyle@igniteproductions.co",
            "junior@igniteproductions.co",
            "keis@igniteproductions.co",
            "harris@igniteproductions.co",
            "nevena@igniteproductions.co",
        }
        assert all(email == email.lower() for email in INTERNAL)

    def test_staffed_plan_alert_skips_the_internal_list(self, sent):
        event = self._activation()
        assert event.request_id

        [internal] = _of(sent, PlanSubmittedInternalMailer)
        assert internal.context["c"].request_code
        assert "/request/view/" in internal.context["c"].request_url

        [alert] = _of(sent, ClientRequestCreatedNotificationMailer)
        recipients = {e.lower() for e in alert.to_emails}
        assert "ops@igniteproductions.co" in recipients
        assert not recipients & INTERNAL

    def test_submitter_on_internal_list_only_gets_the_internal_email(self, sent):
        self._seeding(user=self.kyle)
        assert _of(sent, PlanSubmittedConfirmationMailer) == []
        [internal] = _of(sent, PlanSubmittedInternalMailer)
        assert internal.to_emails.count("kyle@igniteproductions.co") == 1

    def test_draft_save_sends_nothing(self, sent):
        self._seeding(submit=False)
        self._activation(submit=False)
        assert sent == []

    def test_submitting_a_draft_later_sends_once(self, sent):
        draft = self._seeding(submit=False)
        assert sent == []
        submit_event(user=self.marketer, event_id=str(draft.uuid), tenant_id=self.tenant.id)
        assert len(_of(sent, PlanSubmittedConfirmationMailer)) == 1
        assert len(_of(sent, PlanSubmittedInternalMailer)) == 1

        sent.clear()
        submit_event(user=self.marketer, event_id=str(draft.uuid), tenant_id=self.tenant.id)
        assert sent == []

    def test_resubmitting_a_booked_activation_sends_nothing(self, sent):
        event = self._activation()
        sent.clear()
        submit_event(user=self.marketer, event_id=str(event.uuid))
        assert sent == []

    def test_brand_not_opted_in_gets_no_plan_emails(self, sent):
        event = em.FieldMarketingEvent.objects.create(
            tenant=self.other,
            created_by=self.marketer,
            market="miami",
            activity="product_seeding",
            name="Other drop",
            starts_on=date(2026, 10, 17),
            status=em.FieldMarketingEvent.STATUS_SUBMITTED,
        )
        notify_plan_submitted(event, self.marketer, _summary_rows(event), None)
        assert sent == []

    def test_opt_in_resolves_exact_slug_then_request_url_name(self):
        assert plan_submit_mail_for(self.tenant) is not None

        class _T:
            slug = "torch-renamed"
            request_url_name = "keee-torch-thc"

        class _Near:
            slug = "torch-thc-test"
            request_url_name = ""

        assert plan_submit_mail_for(_T()) is not None
        assert plan_submit_mail_for(_Near()) is None
        assert plan_submit_mail_for(self.other) is None

    def test_emails_render_with_the_right_links(self, sent):
        self._activation()
        [confirm] = _of(sent, PlanSubmittedConfirmationMailer)
        [internal] = _of(sent, PlanSubmittedInternalMailer)

        client_html = confirm.render_template()
        assert "https://client.igniteproductions.co/field-marketing" in client_html
        assert "admin.igniteproductions.co/field-marketing" not in client_html
        assert "Montrose block party" in client_html
        assert "2 days" in client_html
        assert not re.search(r"\bapp\b", client_html, re.IGNORECASE)

        internal_html = internal.render_template()
        assert "https://admin.igniteproductions.co/field-marketing" in internal_html
        assert "/request/view/" in internal_html
        assert "Alec Aparicio" in internal_html
        assert "#c5f546" in internal_html
