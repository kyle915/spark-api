"""Walk-up recap autosave is a draft, not a submit.

A BA who loses the page must get their answers back. Saving those answers
must not file the recap, flip approved, write metrics, or send the
recap-ready email. An empty clock-out stub stays unfiled. A recap that
already has submitted content is left alone.
"""

from __future__ import annotations

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from ambassadors.walkup_draft import save_walkup_recap_draft
from recaps.filed import has_filed_recap


@pytest.mark.django_db(transaction=True)
class TestCheckinRecapDraft(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from recaps.models import CustomRecapFieldType, CustomRecapTemplate

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Draft Tenant")
        self.actor = self.create_user(
            username="actor-draft@test.com",
            email="actor-draft@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-draft@test.com",
            email="ba-draft@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Draft template",
            event_type=etype,
            created_by=self.actor,
        )
        self.event = self.create_event(
            name="Thursday shift",
            tenant=self.tenant,
            address="1 Market St",
            event_type=etype,
            date=timezone.now(),
        )
        self.event.custom_recap_template = self.template
        self.event.walkup_code = "DR-DRAFT1"
        self.event.save(update_fields=["custom_recap_template", "walkup_code"])
        CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.actor}
        )
        self.http = DjangoClient()

    def _draft(self, **overrides):
        body = {
            "v": 1,
            "savedAt": 1_700_000_000_000,
            "values": {"notes": "Busy aisle"},
            "samples": {"sku-1": "12"},
            "engagements": "40",
            "usedCorpoCard": False,
        }
        body.update(overrides)
        return body

    def _recaps(self):
        from recaps.models import CustomRecap

        return CustomRecap.objects.filter(event=self.event, ambassador=self.ba)

    def test_draft_save_does_not_create_or_file_a_recap(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            checkin_web,
            "_finalize_recap_offthread",
            lambda recap_id: calls.append(recap_id),
        )

        result = save_walkup_recap_draft(
            event=self.event,
            ambassador=self.ba,
            scope="shift",
            draft=self._draft(),
        )

        assert result["saved"] is True
        assert result["filed"] is False
        assert result["approved"] is False
        assert result["linkedRecap"] is False
        assert self._recaps().count() == 0
        assert calls == []
        assert not has_filed_recap(
            ambassador_id=self.ba.id, event_id=self.event.id
        )

    def test_empty_stub_stays_unfiled_when_a_draft_is_linked(self):
        from recaps.models import CustomFieldValue, CustomRecap

        stub = CustomRecap.objects.create(
            name="Thursday shift",
            event=self.event,
            ambassador=self.ba,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.actor,
            updated_by=self.actor,
            approved=False,
        )

        result = save_walkup_recap_draft(
            event=self.event,
            ambassador=self.ba,
            scope="shift",
            draft=self._draft(),
        )

        stub.refresh_from_db()
        assert result["linkedRecap"] is True
        assert stub.approved is False
        assert stub.submitted_at is None
        assert stub.total_engagements is None
        assert CustomFieldValue.objects.filter(custom_recap=stub).count() == 0
        assert not has_filed_recap(
            ambassador_id=self.ba.id, event_id=self.event.id
        )

    def test_a_filed_recap_is_not_rewritten(self):
        from recaps.models import CustomRecap

        filed_at = timezone.now()
        recap = CustomRecap.objects.create(
            name="Filed Thursday",
            event=self.event,
            ambassador=self.ba,
            tenant=self.tenant,
            custom_recap_template=self.template,
            created_by=self.actor,
            updated_by=self.actor,
            approved=False,
            submitted_at=filed_at,
            total_engagements=9,
        )
        updated_at = recap.updated_at

        result = save_walkup_recap_draft(
            event=self.event,
            ambassador=self.ba,
            scope="additional",
            draft=self._draft(values={"notes": "second stop"}),
        )

        recap.refresh_from_db()
        assert result["linkedRecap"] is False
        assert recap.name == "Filed Thursday"
        assert recap.total_engagements == 9
        assert recap.approved is False
        assert recap.submitted_at == filed_at
        assert recap.updated_at == updated_at
        assert self._recaps().count() == 1

    def test_endpoint_saves_a_draft_and_the_context_returns_it(self):
        from events.checkin_tokens import make_checkin_session_token

        token = make_checkin_session_token(self.event.id, self.ba.id)
        res = self.http.post(
            reverse(
                "events.public_checkin_recap_draft",
                kwargs={"code": self.event.walkup_code},
            ),
            data={
                "session": token,
                "scope": "shift",
                "draft": self._draft(),
            },
            content_type="application/json",
        )

        assert res.status_code == 200
        body = res.json()
        assert body["saved"] is True
        assert body["filed"] is False
        assert body["approved"] is False
        assert "Recap submitted" not in (body.get("message") or "")
        assert self._recaps().count() == 0

        ctx = self.http.get(
            reverse(
                "events.public_checkin_context",
                kwargs={"code": self.event.walkup_code},
            ),
            HTTP_X_CHECKIN_SESSION=token,
        )
        assert ctx.status_code == 200
        payload = ctx.json()
        assert payload["session"]["hasRecap"] is False
        assert payload["session"]["recapDraft"]["values"]["notes"] == "Busy aisle"
        assert payload["session"]["recapDraft"]["engagements"] == "40"

    def test_a_new_scope_does_not_replace_the_open_shift_draft(self):
        save_walkup_recap_draft(
            event=self.event,
            ambassador=self.ba,
            scope="shift",
            draft=self._draft(values={"notes": "thursday"}),
        )
        save_walkup_recap_draft(
            event=self.event,
            ambassador=self.ba,
            scope="additional",
            draft=self._draft(values={"notes": "second stop"}),
        )

        ctx = checkin_web.build_public_context(self.event, self.ba)
        assert ctx["session"]["recapDraft"]["values"]["notes"] == "thursday"
        assert ctx["session"]["additionalRecapDraft"]["values"]["notes"] == "second stop"
        assert ctx["session"]["hasRecap"] is False
