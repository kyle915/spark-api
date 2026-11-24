"""The recap check on the public check-in form is advice, never a gate.

`POST /checkin/<code>/recap-check` looks at the answers a BA is about to
submit and returns a short list of things to fix or confirm. These tests go
through the real view and pin that:

* the number rules fire with no model at all;
* a slow, failing or unconfigured model falls through to the rule findings;
* the model only ever sees field labels and answers;
* a template whose wording matches no matcher gets no number findings;
* the endpoint is rate limited, and a recap still files after any outcome.

The OpenAI call is always mocked at ``utils.ai_text._post_json``.
"""

from __future__ import annotations

import json
import time

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from recaps import recap_precheck


def _completion(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


class _RecapCheckForm(AmbassadorsGraphQLTestCase):
    """The check-in form, event and helpers both test classes use."""

    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            RecapSection,
        )

        cache.clear()
        settings.OPENAI_API_KEY = "test-key"
        settings.OPENAI_MODEL = "gpt-4o-mini"

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Check Tenant")
        self.actor = self.create_user(
            username="actor-check@test.com",
            email="actor-check@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-check@test.com",
            email="ba-check@test.com",
            role=self.roles["ambassador"],
            first_name="Marisol",
            last_name="Quintanilla",
        )
        self.ba = self.create_ambassador(ba_user, phone="555-867-5309")
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Check template",
            event_type=etype,
            created_by=self.actor,
        )
        self.event = self.create_event(
            name="Total Wine Houston",
            tenant=self.tenant,
            address="1 Market St",
            event_type=etype,
            date=timezone.now(),
        )
        self.event.custom_recap_template = self.template
        self.event.walkup_code = "CK-CHECK1"
        self.event.save(update_fields=["custom_recap_template", "walkup_code"])

        number, _ = CustomRecapFieldType.objects.get_or_create(
            name="number", defaults={"created_by": self.actor}
        )
        text, _ = CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.actor}
        )
        self.section = RecapSection.objects.create(
            tenant=self.tenant, name="Results", order=0, created_by=self.actor
        )

        def field(name, kind, order, required=False):
            return CustomField.objects.create(
                name=name,
                custom_recap_template=self.template,
                custom_field_type=kind,
                recap_section=self.section,
                order=order,
                required=required,
                created_by=self.actor,
            )

        self.sampled = field("Total number of consumers sampled", number, 0, True)
        self.cans = field("Single cans sold", number, 1)
        self.willing = field("Consumers willing to purchase", number, 2)
        self.feedback = field("Consumer feedback", text, 3, True)
        self.notes = field("Additional notes", text, 4)
        self.ba_name = field("Brand ambassador name", text, 5)
        self.http = DjangoClient()
        self.token = make_checkin_session_token(self.event.id, self.ba.id)

    # ---------- helpers ----------

    def _values(self, **by_field):
        ids = {
            "sampled": self.sampled.id,
            "cans": self.cans.id,
            "willing": self.willing.id,
            "feedback": self.feedback.id,
            "notes": self.notes.id,
            "ba_name": self.ba_name.id,
        }
        return [
            {"customFieldId": str(ids[key]), "value": str(value)}
            for key, value in by_field.items()
        ]

    def _check(self, field_values, **extra):
        return self.http.post(
            reverse(
                "events.public_checkin_recap_check",
                kwargs={"code": self.event.walkup_code},
            ),
            data={"session": self.token, "fieldValues": field_values, **extra},
            content_type="application/json",
        )

    @staticmethod
    def _codes(res):
        return [item["code"] for item in res.json()["items"]]

    def _no_model(self, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("the model must not be called")

        monkeypatch.setattr("utils.ai_text._post_json", boom)

    GOOD_FEEDBACK = (
        "Shoppers liked the mango flavor and two asked where it is shelved."
    )



@pytest.mark.django_db(transaction=True)
class TestCheckinRecapCheck(_RecapCheckForm):
    # ---------- rules, no model ----------

    def test_sold_more_than_sampled_is_one_plain_line(self, monkeypatch):
        self._no_model(monkeypatch)
        res = self._check(self._values(sampled=40, cans=55), useless=1)

        assert res.status_code == 200
        items = res.json()["items"]
        assert [i["code"] for i in items] == ["sold_exceeds_sampled"]
        assert items[0]["message"] == (
            "You sampled 40 people but sold 55 cans. Is that right?"
        )
        assert items[0]["fieldId"] == str(self.cans.id)
        assert res.json()["modelChecked"] is False

    def test_willing_to_buy_more_than_sampled(self, monkeypatch):
        self._no_model(monkeypatch)
        res = self._check(self._values(sampled=30, willing=45))

        assert self._codes(res) == ["willing_exceeds_sampled"]
        assert res.json()["items"][0]["message"] == (
            "You sampled 30 people but 45 were willing to buy. Is that right?"
        )

    def test_zero_sampled_with_photos(self, monkeypatch):
        self._no_model(monkeypatch)
        with_photos = self._check(self._values(sampled=0), photoCount=3)
        without = self._check(self._values(sampled=0), photoCount=0)

        assert self._codes(with_photos) == ["zero_sampled_with_photos"]
        assert "3 photos" in with_photos.json()["items"][0]["message"]
        assert self._codes(without) == []

    def test_required_number_left_blank(self, monkeypatch):
        self._no_model(monkeypatch)
        res = self._check(self._values(cans=4))

        assert "number_missing" in self._codes(res)
        missing = [i for i in res.json()["items"] if i["code"] == "number_missing"]
        assert missing[0]["fieldId"] == str(self.sampled.id)

    def test_count_too_high_for_the_shift(self, monkeypatch):
        self._no_model(monkeypatch)
        start = timezone.now()
        self.event.start_time = start
        self.event.end_time = start + timezone.timedelta(hours=4)
        self.event.save(update_fields=["start_time", "end_time"])

        res = self._check(self._values(sampled=4000))

        assert self._codes(res) == ["count_too_high_for_shift"]
        assert "4h shift" in res.json()["items"][0]["message"]

    def test_one_word_feedback_is_caught_without_the_model(self, monkeypatch):
        self._no_model(monkeypatch)
        res = self._check(self._values(sampled=40, feedback="n/a"))

        assert self._codes(res) == ["thin_answer"]
        assert res.json()["items"][0]["fieldId"] == str(self.feedback.id)

    def test_optional_notes_may_say_none(self, monkeypatch):
        self._no_model(monkeypatch)
        res = self._check(self._values(sampled=40, notes="none"))

        assert self._codes(res) == []

    def test_clean_answers_return_nothing(self, monkeypatch):
        self._no_model(monkeypatch)
        res = self._check(self._values(sampled=40, cans=12, willing=20))

        assert res.status_code == 200
        assert res.json() == {"items": [], "modelChecked": False}

    def test_unmatched_wording_skips_the_number_rules(self, monkeypatch):
        """A template that words its numbers differently gets no guess."""
        from recaps.models import CustomField

        self._no_model(monkeypatch)
        CustomField.objects.filter(id=self.sampled.id).update(
            name="Folks at the table", required=False
        )
        CustomField.objects.filter(id=self.cans.id).update(name="Bottles out the door")

        res = self._check(self._values(sampled=40, cans=55))

        assert self._codes(res) == []

    # ---------- the model step ----------

    def test_model_flags_empty_feedback_and_a_number_conflict(self, monkeypatch):
        sent = []

        def fake_post(url, payload, headers, *, timeout):
            sent.append((payload, timeout))
            return _completion(
                {
                    "issues": [
                        {"text": "t1", "kind": "empty", "number": None},
                        {"text": "t2", "kind": "number_conflict", "number": "n2"},
                    ]
                }
            )

        monkeypatch.setattr("utils.ai_text._post_json", fake_post)
        res = self._check(
            self._values(
                sampled=40,
                cans=12,
                feedback="it was good",
                notes="Busy day, sold about 55 cans by the end",
                ba_name="Marisol Quintanilla",
            )
        )

        body = res.json()
        assert [i["code"] for i in body["items"]] == ["thin_answer", "number_conflict"]
        assert body["modelChecked"] is True
        conflict = body["items"][1]
        assert conflict["fieldId"] == str(self.notes.id)
        assert conflict["message"] == (
            "“Additional notes” mentions a different number than “Single "
            "cans sold” (12). Can you check which is right?"
        )
        # One call, the smallest completion ceiling utils.ai_text allows, and
        # a short per-attempt timeout. Time, not tokens, bounds the wait.
        from utils import ai_text

        assert len(sent) == 1
        payload, timeout = sent[0]
        assert payload["max_completion_tokens"] == ai_text._MIN_COMPLETION_TOKENS
        assert timeout <= recap_precheck.MODEL_DEADLINE_SECONDS
        # The page stops waiting at 5 seconds; the server gives up first.
        assert recap_precheck.MODEL_DEADLINE_SECONDS < 5

    def test_a_raised_token_floor_still_fails_open_inside_the_limit(self, monkeypatch):
        """Whatever completion ceiling utils.ai_text applies, a model that is
        still writing when the limit passes is dropped, not waited for."""
        monkeypatch.setattr(recap_precheck, "MODEL_DEADLINE_SECONDS", 0.2)
        seen = []

        def slow_post(url, payload, headers, *, timeout):
            seen.append(timeout)
            payload["max_completion_tokens"] = 4000
            time.sleep(1.0)
            return _completion(
                {"issues": [{"text": "t1", "kind": "empty", "number": None}]}
            )

        monkeypatch.setattr("utils.ai_text._post_json", slow_post)
        started = time.monotonic()
        res = self._check(self._values(sampled=40, feedback="it was good"))

        assert res.status_code == 200
        assert res.json() == {"items": [], "modelChecked": False}
        assert time.monotonic() - started < 0.9
        assert seen and all(t <= 3.5 for t in seen)

    def test_model_sees_labels_and_answers_only(self, monkeypatch):
        sent = []

        def fake_post(url, payload, headers, *, timeout):
            sent.append(payload)
            return _completion({"issues": []})

        monkeypatch.setattr("utils.ai_text._post_json", fake_post)
        self._check(
            self._values(
                sampled=40,
                feedback="Call me on 555-867-5309 or ba-check@test.com, it went well",
                ba_name="Marisol Quintanilla",
            )
        )

        assert len(sent) == 1
        prompt = json.dumps(sent[0])
        for private in (
            "Marisol",
            "Quintanilla",
            "867-5309",
            "ba-check@test.com",
            "Total Wine Houston",
            "1 Market St",
            self.token,
            "Brand ambassador name",
        ):
            assert private not in prompt
        assert "Consumer feedback" in prompt
        assert "it went well" in prompt

    def test_model_cannot_invent_a_conflict(self, monkeypatch):
        """A conflict is kept only when the text really carries another number."""

        def fake_post(url, payload, headers, *, timeout):
            return _completion(
                {
                    "issues": [
                        {"text": "t1", "kind": "number_conflict", "number": "n2"},
                        {"text": "t9", "kind": "empty", "number": None},
                    ]
                }
            )

        monkeypatch.setattr("utils.ai_text._post_json", fake_post)
        res = self._check(
            self._values(sampled=40, cans=12, notes="We sold 12 cans, steady flow")
        )

        assert self._codes(res) == []

    def test_no_model_call_when_there_is_nothing_to_judge(self, monkeypatch):
        self._no_model(monkeypatch)
        res = self._check(self._values(sampled=40, cans=12))

        assert res.json() == {"items": [], "modelChecked": False}

    def test_slow_model_is_dropped_and_rules_still_answer(self, monkeypatch):
        monkeypatch.setattr(recap_precheck, "MODEL_DEADLINE_SECONDS", 0.2)

        def slow_post(url, payload, headers, *, timeout):
            time.sleep(1.5)
            return _completion(
                {"issues": [{"text": "t1", "kind": "empty", "number": None}]}
            )

        monkeypatch.setattr("utils.ai_text._post_json", slow_post)
        started = time.monotonic()
        res = self._check(
            self._values(sampled=40, cans=55, feedback="it was good")
        )
        elapsed = time.monotonic() - started

        assert res.status_code == 200
        assert self._codes(res) == ["sold_exceeds_sampled"]
        assert res.json()["modelChecked"] is False
        assert elapsed < 1.2

    def test_model_error_falls_through(self, monkeypatch):
        from utils.ai_text import AiUnavailable

        def broken_post(url, payload, headers, *, timeout):
            raise AiUnavailable("AI service timed out.")

        monkeypatch.setattr("utils.ai_text._post_json", broken_post)
        res = self._check(
            self._values(sampled=40, cans=55, feedback="it was good")
        )

        assert res.status_code == 200
        assert self._codes(res) == ["sold_exceeds_sampled"]
        assert res.json()["modelChecked"] is False

    def test_unconfigured_model_falls_through(self, monkeypatch, settings):
        settings.OPENAI_API_KEY = ""
        self._no_model(monkeypatch)
        res = self._check(
            self._values(sampled=40, cans=55, feedback="it was good")
        )

        assert res.status_code == 200
        assert self._codes(res) == ["sold_exceeds_sampled"]

    def test_garbage_from_the_model_is_ignored(self, monkeypatch):
        monkeypatch.setattr(
            "utils.ai_text._post_json",
            lambda *a, **k: {"choices": [{"message": {"content": "not json"}}]},
        )
        res = self._check(self._values(sampled=40, feedback="it was good"))

        assert res.status_code == 200
        assert self._codes(res) == []

    # ---------- public endpoint rules ----------

    def test_needs_the_form_session(self, monkeypatch):
        self._no_model(monkeypatch)
        res = self.http.post(
            reverse(
                "events.public_checkin_recap_check",
                kwargs={"code": self.event.walkup_code},
            ),
            data={"fieldValues": self._values(sampled=40, cans=55)},
            content_type="application/json",
        )

        assert res.status_code == 401

    def test_rate_limited_per_network(self, monkeypatch):
        self._no_model(monkeypatch)
        values = self._values(sampled=40, cans=12)
        statuses = [self._check(values).status_code for _ in range(41)]

        assert statuses[:40] == [200] * 40
        assert statuses[40] == 429
        assert self._check(values).json()["error"] == "rate_limited"

    def test_model_budget_runs_out_before_the_rules_do(self, monkeypatch):
        calls = []

        def fake_post(url, payload, headers, *, timeout):
            calls.append(1)
            return _completion({"issues": []})

        monkeypatch.setattr("utils.ai_text._post_json", fake_post)
        values = self._values(sampled=40, cans=55, feedback="it was good")
        results = [self._check(values) for _ in range(18)]

        assert len(calls) == 15
        assert all(self._codes(r) == ["sold_exceeds_sampled"] for r in results)

    def test_check_writes_nothing_and_submit_still_files(self, monkeypatch):
        """Whatever the check said, the recap can be filed as is."""
        from ambassadors import checkin_web
        from recaps.models import CustomRecap, FileRecapCategory, FileType

        FileType.objects.get_or_create(
            name="image", defaults={"created_by": self.actor}
        )
        FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=self.actor
        )
        self._no_model(monkeypatch)
        monkeypatch.setattr(
            checkin_web, "_finalize_recap_offthread", lambda recap_id: None
        )
        values = self._values(sampled=40, cans=55, feedback="n/a")
        res = self._check(values, photoCount=1)

        assert len(res.json()["items"]) == 2
        assert CustomRecap.objects.filter(event=self.event).count() == 0

        blob = f"recap_files/checkin/{self.event.uuid}/photo.jpg"
        filed = self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": self.event.walkup_code},
            ),
            data={
                "session": self.token,
                "fieldValues": values,
                "files": [{"blobName": blob}],
                "usedCorpoCard": False,
            },
            content_type="application/json",
        )

        assert filed.status_code == 200, filed.content
        assert filed.json()["success"] is True
        assert CustomRecap.objects.filter(event=self.event).count() == 1


# ---------------------------------------------------------------------------
# Contradictions within one recap, kept on the recap at submit
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestRecapContradictions(_RecapCheckForm):
    """Answers that cannot all be right, on the same form and event."""

    def _field(self, name, kind="number", required=False):
        from recaps.models import CustomField, CustomRecapFieldType

        ftype, _ = CustomRecapFieldType.objects.get_or_create(
            name=kind, defaults={"created_by": self.actor}
        )
        return CustomField.objects.create(
            name=name,
            custom_recap_template=self.template,
            custom_field_type=ftype,
            recap_section=self.section,
            order=20,
            required=required,
            created_by=self.actor,
        )

    @staticmethod
    def _rows(*pairs):
        return [{"customFieldId": str(f.id), "value": str(v)} for f, v in pairs]

    def _item(self, res, code):
        found = [i for i in res.json()["items"] if i["code"] == code]
        assert found, f"{code} not in {self._codes(res)}"
        return found[0]

    # ---------- sales ----------

    def test_total_sold_zero_with_lines_above_zero(self, monkeypatch):
        self._no_model(monkeypatch)
        total = self._field("Total cans sold")
        res = self._check(self._rows((self.sampled, 40), (self.cans, 5), (total, 0)))

        item = self._item(res, "sold_total_zero")
        assert item["severity"] == "error"
        assert item["message"] == (
            "You entered 5 for “Single cans sold” but 0 for “Total cans sold”. "
            "Is that right?"
        )
        assert item["fieldId"] == str(total.id)
        assert set(item["fieldIds"]) == {str(total.id), str(self.cans.id)}
        assert "staff" not in item

    def test_a_flavor_that_sold_zero_is_an_ordinary_day(self, monkeypatch):
        self._no_model(monkeypatch)
        packs = self._field("Packs sold")
        res = self._check(self._rows((self.sampled, 40), (self.cans, 5), (packs, 0)))

        assert self._codes(res) == []

    def test_confirmed_upgrade_line_against_zero_tins_sold(self, monkeypatch):
        """American Ice: 3 "4-for-$6 upgrades" (staff-confirmed units sold)
        and 0 "$1 tins sold"."""
        from recaps.models import CustomField, RecapFieldMetricMapping

        self._no_model(monkeypatch)
        CustomField.objects.filter(id=self.cans.id).delete()
        upgrades = self._field("4-for-$6 upgrades")
        tins = self._field("$1 tins sold")
        RecapFieldMetricMapping.objects.create(
            custom_field=upgrades,
            metric="units_sold_pack",
            confirmed_at=timezone.now(),
        )
        res = self._check(self._rows((self.sampled, 40), (upgrades, 3), (tins, 0)))

        item = self._item(res, "sold_line_zero")
        assert item["severity"] == "warning"
        assert item["message"] == (
            "You entered 3 for “4-for-$6 upgrades” but 0 for “$1 tins sold”. "
            "Is that right?"
        )
        assert set(item["fieldIds"]) == {str(tins.id), str(upgrades.id)}

    def test_sold_with_nobody_sampled(self, monkeypatch):
        self._no_model(monkeypatch)
        res = self._check(self._values(sampled=0, cans=6), photoCount=2)

        item = self._item(res, "sold_without_consumers")
        assert item["message"] == (
            "You sold 6 cans but entered 0 people sampled. Is that right?"
        )
        # One finding about the zero, not two.
        assert "zero_sampled_with_photos" not in self._codes(res)

    def test_dry_demo_may_sell_without_sampling(self, monkeypatch):
        self._no_model(monkeypatch)
        dry = self._field("Dry demo?", kind="text")
        res = self._check(
            self._values(sampled=0, cans=6) + self._rows((dry, "Yes"))
        )

        assert "sold_without_consumers" not in self._codes(res)

    # ---------- shares of the people sampled ----------

    def test_first_time_and_brand_aware_over_sampled(self, monkeypatch):
        self._no_model(monkeypatch)
        first = self._field("First time consumers")
        knew = self._field("Consumers who knew about the brand")
        res = self._check(
            self._rows((self.sampled, 30), (first, 45), (knew, 31), (self.cans, 2))
        )

        assert self._codes(res)[:2] == [
            "first_time_exceeds_sampled",
            "brand_aware_exceeds_sampled",
        ]
        item = self._item(res, "first_time_exceeds_sampled")
        assert item["severity"] == "error"
        assert item["message"] == (
            "You sampled 30 people but entered 45 for “First time consumers”. "
            "Is that right?"
        )
        assert set(item["fieldIds"]) == {str(first.id), str(self.sampled.id)}

    def test_shares_within_sampled_pass(self, monkeypatch):
        self._no_model(monkeypatch)
        first = self._field("First time consumers")
        res = self._check(self._rows((self.sampled, 30), (first, 30)))

        assert self._codes(res) == []

    # ---------- samples and swag on hand ----------

    def test_gave_out_more_than_on_hand(self, monkeypatch):
        self._no_model(monkeypatch)
        start = self._field("Samples on hand at start")
        given = self._field("Samples given out")
        res = self._check(self._rows((self.sampled, 40), (start, 50), (given, 60)))

        item = self._item(res, "given_exceeds_on_hand")
        assert item["severity"] == "error"
        assert item["message"] == (
            "You gave out 60 samples but had 50 at the start. Is that right?"
        )

    def test_end_count_must_be_start_minus_given(self, monkeypatch):
        self._no_model(monkeypatch)
        start = self._field("Swag starting inventory")
        given = self._field("Swag handed out")
        end = self._field("Swag remaining")
        bad = self._check(
            self._rows((self.sampled, 40), (start, 100), (given, 60), (end, 30))
        )
        good = self._check(
            self._rows((self.sampled, 40), (start, 100), (given, 60), (end, 40))
        )

        item = self._item(bad, "stock_does_not_add_up")
        assert item["message"] == (
            "You started with 100 swag, gave out 60 and ended with 30. "
            "100 minus 60 is 40. Is that right?"
        )
        assert item["fieldId"] == str(end.id)
        assert self._codes(good) == []

    def test_cups_poured_are_not_compared_with_cans_on_hand(self, monkeypatch):
        self._no_model(monkeypatch)
        start = self._field("Cans on hand at start")
        given = self._field("Samples given out")
        res = self._check(self._rows((self.sampled, 40), (start, 12), (given, 150)))

        assert "given_exceeds_on_hand" not in self._codes(res)

    # ---------- receipts ----------

    def test_spend_against_the_receipts_read_on_the_page(self, monkeypatch):
        self._no_model(monkeypatch)
        spend = self._field("Product spend")
        off = self._check(
            self._rows((self.sampled, 40), (spend, "60.00")),
            receiptSpend={"total": "42.10"},
        )
        close = self._check(
            self._rows((self.sampled, 40), (spend, "42.50")),
            receiptSpend={"total": "42.10"},
        )
        no_receipts = self._check(self._rows((self.sampled, 40), (spend, "60.00")))

        item = self._item(off, "spend_vs_receipts")
        assert item["message"] == (
            "Your receipts add up to $42.10 but you entered $60.00 for "
            "“Product spend”. Is that right?"
        )
        assert item["fieldId"] == str(spend.id)
        assert self._codes(close) == []
        assert self._codes(no_receipts) == []

    # ---------- clock ----------

    def _punch(self, kind, when):
        from ambassadors.models import Attendance, Source

        source, _ = Source.objects.get_or_create(name=kind)
        Attendance.objects.create(
            clock_time=when, event=self.event, ambassador=self.ba, source=source
        )

    def _schedule(self, hours):
        start = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0)
        self.event.start_time = start
        self.event.end_time = start + timezone.timedelta(hours=hours)
        self.event.save(update_fields=["start_time", "end_time"])
        return start

    def test_clock_far_past_the_shift(self, monkeypatch):
        self._no_model(monkeypatch)
        start = self._schedule(4)
        self._punch("clock_in", start)
        self._punch("clock_out", start + timezone.timedelta(hours=9))
        res = self._check(self._values(sampled=40, cans=12))

        item = self._item(res, "clock_vs_schedule")
        assert item["message"].startswith("You were on the clock 9h for a 4h shift.")

    def test_clock_well_short_of_the_shift(self, monkeypatch):
        self._no_model(monkeypatch)
        start = self._schedule(4)
        self._punch("clock_in", start)
        self._punch("clock_out", start + timezone.timedelta(hours=2))
        res = self._check(self._values(sampled=40, cans=12))

        assert self._item(res, "clock_vs_schedule")["message"].startswith(
            "You were on the clock 2h for a 4h shift."
        )

    def test_clock_within_tolerance_says_nothing(self, monkeypatch):
        self._no_model(monkeypatch)
        start = self._schedule(4)
        self._punch("clock_in", start + timezone.timedelta(minutes=10))
        self._punch("clock_out", start + timezone.timedelta(hours=3, minutes=10))
        res = self._check(self._values(sampled=40, cans=12))

        assert self._codes(res) == []

    def test_clock_out_before_clock_in(self, monkeypatch):
        self._no_model(monkeypatch)
        start = self._schedule(4)
        self._punch("clock_in", start + timezone.timedelta(hours=2))
        self._punch("clock_out", start)
        res = self._check(self._values(sampled=40, cans=12))

        item = self._item(res, "clock_out_before_in")
        assert item["severity"] == "error"

    def test_no_clock_out_yet_says_nothing(self, monkeypatch):
        self._no_model(monkeypatch)
        start = self._schedule(4)
        self._punch("clock_in", start)
        res = self._check(self._values(sampled=40, cans=12))

        assert self._codes(res) == []

    # ---------- the brand's own history ----------

    def _history(self, field, values):
        from recaps.models import CustomFieldValue, CustomRecap

        for n, value in enumerate(values):
            recap = CustomRecap.objects.create(
                name=f"past-{n}",
                event=self.event,
                tenant=self.tenant,
                custom_recap_template=self.template,
                created_by=self.actor,
                approved=True,
            )
            CustomFieldValue.objects.create(
                custom_recap=recap,
                custom_field=field,
                value=str(value),
                created_by=self.actor,
            )

    def test_far_above_this_programs_usual(self, monkeypatch):
        self._no_model(monkeypatch)
        self._history(self.sampled, [30] * 25)
        res = self._check(self._values(sampled=400, cans=12))

        item = self._item(res, "far_above_usual")
        assert item["severity"] == "warning"
        # The ambassador never sees the brand's usual number.
        assert "30" not in json.dumps(item)
        assert item["message"] == (
            "“Total number of consumers sampled” is 400, much higher than "
            "usual for this program. Can you check for an extra zero?"
        )

    def test_history_needs_enough_recaps(self, monkeypatch):
        self._no_model(monkeypatch)
        self._history(self.sampled, [40] * 10)
        res = self._check(self._values(sampled=400, cans=12))

        assert "far_above_usual" not in self._codes(res)

    def test_history_needs_a_real_gap(self, monkeypatch):
        self._no_model(monkeypatch)
        self._history(self.sampled, [2] * 25)
        res = self._check(self._values(sampled=15, cans=1))

        assert "far_above_usual" not in self._codes(res)

    # ---------- kept on the recap, shown to the reviewer ----------

    def _submit(self, field_values, **extra):
        from recaps.models import FileRecapCategory, FileType

        FileType.objects.get_or_create(name="image", defaults={"created_by": self.actor})
        FileRecapCategory.objects.get_or_create(
            name="Sampling photos", tenant=self.tenant,
            defaults={"created_by": self.actor},
        )
        blob = f"recap_files/checkin/{self.event.uuid}/photo.jpg"
        return self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": self.event.walkup_code},
            ),
            data={
                "session": self.token,
                "fieldValues": field_values,
                "files": [{"blobName": blob}],
                "usedCorpoCard": False,
                **extra,
            },
            content_type="application/json",
        )

    def test_submit_keeps_the_findings_and_what_the_ba_confirmed(self, monkeypatch):
        from ambassadors import checkin_web
        from recaps.models import CustomRecap
        from recaps.recap_review_checks import recap_review_checks
        from recaps.review_triage import recap_review_triage

        self._no_model(monkeypatch)
        monkeypatch.setattr(checkin_web, "_finalize_recap_offthread", lambda rid: None)
        first = self._field("First time consumers")
        values = self._values(sampled=40, cans=55, feedback=self.GOOD_FEEDBACK) + (
            self._rows((first, 41))
        )
        filed = self._submit(
            values,
            checkConfirmed=["sold_exceeds_sampled", "made_up_code"],
            checkShown=["sold_exceeds_sampled", "first_time_exceeds_sampled"],
        )

        assert filed.status_code == 200, filed.content
        recap = CustomRecap.objects.get(event=self.event)
        stored = {i["code"]: i for i in recap.submit_checks["items"]}
        assert set(stored) == {"sold_exceeds_sampled", "first_time_exceeds_sampled"}
        assert stored["sold_exceeds_sampled"]["confirmed"] is True
        assert stored["sold_exceeds_sampled"]["staff"] == "55 cans sold, 40 sampled"
        assert stored["first_time_exceeds_sampled"]["confirmed"] is False
        assert stored["first_time_exceeds_sampled"]["shown"] is True
        assert stored["first_time_exceeds_sampled"]["severity"] == "error"

        checks = {c["code"]: c for c in recap_review_checks(recap.id, is_custom=True)}
        confirmed = checks["ba_check_sold_exceeds_sampled"]
        assert confirmed["status"] == "flag"
        assert confirmed["label"] == "BA confirmed: 55 cans sold, 40 sampled"
        assert "The BA was asked: You sampled 40 people" in confirmed["detail"]
        assert checks["ba_check_first_time_exceeds_sampled"]["label"] == (
            "BA submitted anyway: 41 first-time, 40 sampled"
        )

        triage = recap_review_triage(recap.id, is_custom=True)
        assert triage["label"] == "needs_eyes"
        assert "ba_check_sold_exceeds_sampled" in {r["code"] for r in triage["reasons"]}

    def test_submit_with_clean_answers_keeps_an_empty_list(self, monkeypatch):
        from ambassadors import checkin_web
        from recaps.models import CustomRecap
        from recaps.recap_review_checks import recap_review_checks

        self._no_model(monkeypatch)
        monkeypatch.setattr(checkin_web, "_finalize_recap_offthread", lambda rid: None)
        filed = self._submit(
            self._values(sampled=40, cans=12, feedback=self.GOOD_FEEDBACK)
        )

        assert filed.status_code == 200, filed.content
        recap = CustomRecap.objects.get(event=self.event)
        assert recap.submit_checks["items"] == []
        assert not any(
            c["code"].startswith("ba_check_")
            for c in recap_review_checks(recap.id, is_custom=True)
        )

    def test_a_broken_check_never_fails_the_submit(self, monkeypatch):
        from ambassadors import checkin_web
        from recaps import recap_precheck as precheck
        from recaps.models import CustomRecap

        self._no_model(monkeypatch)
        monkeypatch.setattr(checkin_web, "_finalize_recap_offthread", lambda rid: None)

        def boom(**_kw):
            raise RuntimeError("check fault")

        monkeypatch.setattr(precheck, "precheck_recap", boom)
        filed = self._submit(self._values(sampled=40, cans=55))

        assert filed.status_code == 200, filed.content
        assert CustomRecap.objects.get(event=self.event).submit_checks == {}

    def test_a_percent_share_is_not_a_head_count(self, monkeypatch):
        self._no_model(monkeypatch)
        first = self._field("First time consumers (%)")
        res = self._check(self._rows((self.sampled, 40), (first, 60)))

        assert "first_time_exceeds_sampled" not in self._codes(res)
