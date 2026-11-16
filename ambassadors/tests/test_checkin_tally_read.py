""""Fill from photo" on the public check-in form proposes numbers, nothing more.

`POST /checkin/<code>/tally-read` reads a photo of a tally sheet and returns
values for the form's NUMBER fields. These tests go through the real view and
pin that:

* only number fields of this template (and its products) can come back;
* a value the model does not mark clearly legible is left out, never guessed;
* an unreadable photo, a slow model, a failing model and no key at all give
  the same plain "could not read" answer;
* the model sees field labels and product names only;
* the same photo is not read twice;
* the endpoint is rate limited, saves nothing, and a recap still files.

The OpenAI call is always mocked at ``utils.ai_text._post_json``.
"""

from __future__ import annotations

import base64
import json
import time
from io import BytesIO

import pytest
from django.core.cache import cache
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from recaps import tally_reader
from recaps.tally_reader import TALLY_UNREADABLE, interpret_tally, parse_count


def _completion(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


def _jpeg(shade: int = 200) -> str:
    out = BytesIO()
    Image.new("RGB", (64, 48), (shade, shade, shade)).save(out, "JPEG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def test_parse_count_takes_plain_plausible_numbers_only():
    assert parse_count("42") == "42"
    assert parse_count("1,250") == "1250"
    assert parse_count("007") == "7"
    assert parse_count("4.5") is None  # a count is a whole number
    assert parse_count("about 40") is None
    assert parse_count("-3") is None
    assert parse_count("5551234567") is None  # a phone number, not a count
    assert parse_count("38.11", money=True) == "38.11"
    assert parse_count("$40", money=True) == "40"


def test_interpret_drops_a_line_answered_two_ways():
    targets = [
        {"ref": "f1", "kind": "field", "id": "9", "label": "Cans sold", "money": False},
        {"ref": "f2", "kind": "field", "id": "10", "label": "Packs sold", "money": False},
    ]
    out = interpret_tally(
        {
            "isTally": True,
            "readings": [
                {"ref": "f1", "value": "12", "legible": True},
                {"ref": "f1", "value": "72", "legible": True},
                {"ref": "f2", "value": "3", "legible": True},
            ],
        },
        targets,
    )
    assert out["values"] == [{"fieldId": "10", "value": "3"}]


@pytest.mark.django_db(transaction=True)
class TestCheckinTallyRead(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from events.models import Product, ProductType
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
        self.tenant = self.create_tenant(name="Tally Tenant")
        self.actor = self.create_user(
            username="actor-tally@test.com",
            email="actor-tally@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-tally@test.com",
            email="ba-tally@test.com",
            role=self.roles["ambassador"],
            first_name="Marisol",
            last_name="Quintanilla",
        )
        self.ba = self.create_ambassador(ba_user, phone="555-867-5309")
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Tally template",
            event_type=etype,
            product_samples=True,
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
        self.event.walkup_code = "CK-TALLY1"
        self.event.save(update_fields=["custom_recap_template", "walkup_code"])

        number, _ = CustomRecapFieldType.objects.get_or_create(
            name="number", defaults={"created_by": self.actor}
        )
        text, _ = CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.actor}
        )
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Results", order=0, created_by=self.actor
        )

        def field(name, kind, order):
            return CustomField.objects.create(
                name=name,
                custom_recap_template=self.template,
                custom_field_type=kind,
                recap_section=section,
                order=order,
                created_by=self.actor,
            )

        self.sampled = field("Total number of consumers sampled", number, 0)
        self.cans = field("Single cans sold", number, 1)
        self.packs = field("4-packs sold", number, 2)
        self.store_number = field("Store number", number, 3)
        self.feedback = field("Consumer feedback", text, 4)
        ptype = ProductType.objects.create(
            name="Seltzer", tenant=self.tenant, created_by=self.actor
        )
        self.mango = Product.objects.create(
            name="Mango", product_type=ptype, tenant=self.tenant, created_by=self.actor
        )
        self.http = DjangoClient()
        self.token = make_checkin_session_token(self.event.id, self.ba.id)

    # ---------- helpers ----------

    def _read(self, image=None, **extra):
        return self.http.post(
            reverse(
                "events.public_checkin_tally_read",
                kwargs={"code": self.event.walkup_code},
            ),
            data={
                "session": self.token,
                "imageBase64": _jpeg() if image is None else image,
                "contentType": "image/jpeg",
                **extra,
            },
            content_type="application/json",
        )

    def _model(self, monkeypatch, readings, is_tally=True):
        calls = []

        def post(url, payload, headers, timeout):
            calls.append(payload)
            return _completion({"isTally": is_tally, "readings": readings})

        monkeypatch.setattr("utils.ai_text._post_json", post)
        return calls

    @staticmethod
    def _refs(payload) -> dict[str, str]:
        """label → ref, from the prompt the model was sent."""
        text = payload["messages"][1]["content"][0]["text"]
        out = {}
        for line in text.splitlines():
            if line.startswith("- ["):
                ref, label = line[3:].split("] ", 1)
                out[label] = ref
        return out

    # ---------- what comes back ----------

    def test_values_come_back_for_number_fields_only(self, monkeypatch):
        seen = []

        def post(url, payload, headers, timeout):
            seen.append(payload)
            refs = self._refs(payload)
            return _completion(
                {
                    "isTally": True,
                    "readings": [
                        {"ref": refs["Total number of consumers sampled"], "value": "48", "legible": True},
                        {"ref": refs["Single cans sold"], "value": "12", "legible": True},
                        # Not a line the form offered.
                        {"ref": "f99", "value": "7", "legible": True},
                    ],
                }
            )

        monkeypatch.setattr("utils.ai_text._post_json", post)
        res = self._read()

        assert res.status_code == 200
        body = res.json()
        assert body["available"] is True
        assert body["values"] == [
            {"fieldId": str(self.sampled.id), "value": "48"},
            {"fieldId": str(self.cans.id), "value": "12"},
        ]
        assert body["productSamples"] == []
        # The text field and the store number were never offered.
        labels = set(self._refs(seen[0]))
        assert labels == {
            "Total number of consumers sampled",
            "Single cans sold",
            "4-packs sold",
        }
        assert "enum" in json.dumps(seen[0]["response_format"])

    def test_products_are_offered_only_when_the_form_asks(self, monkeypatch):
        def post(url, payload, headers, timeout):
            refs = self._refs(payload)
            return _completion(
                {
                    "isTally": True,
                    "readings": [
                        {"ref": refs["count for product: Mango"], "value": "18", "legible": True}
                    ],
                }
            )

        monkeypatch.setattr("utils.ai_text._post_json", post)
        res = self._read(includeProducts=True)

        assert res.json()["productSamples"] == [
            {"productId": str(self.mango.id), "value": "18"}
        ]
        assert res.json()["values"] == []

    def test_a_number_that_is_not_clearly_legible_is_left_blank(self, monkeypatch):
        def post(url, payload, headers, timeout):
            refs = self._refs(payload)
            return _completion(
                {
                    "isTally": True,
                    "readings": [
                        {"ref": refs["Single cans sold"], "value": "12", "legible": False},
                        {"ref": refs["4-packs sold"], "value": "3 or 8", "legible": True},
                        {"ref": refs["Total number of consumers sampled"], "value": "40", "legible": True},
                    ],
                }
            )

        monkeypatch.setattr("utils.ai_text._post_json", post)
        body = self._read().json()

        assert body["values"] == [{"fieldId": str(self.sampled.id), "value": "40"}]

    def test_unreadable_photo_is_blank_with_the_plain_line(self, monkeypatch):
        self._model(monkeypatch, [], is_tally=False)
        body = self._read().json()

        assert body == {
            "available": False,
            "values": [],
            "productSamples": [],
            "message": TALLY_UNREADABLE,
        }
        assert TALLY_UNREADABLE == "Could not read that photo. Enter the numbers by hand."

    def test_nothing_legible_is_the_same_plain_line(self, monkeypatch):
        self._model(monkeypatch, [{"ref": "f1", "value": "", "legible": False}])
        assert self._read().json()["available"] is False

    # ---------- privacy ----------

    def test_model_sees_labels_and_product_names_only(self, monkeypatch):
        calls = self._model(monkeypatch, [])
        self._read(includeProducts=True)

        sent = json.dumps(calls[0]["messages"][0]) + calls[0]["messages"][1]["content"][0]["text"]
        for private in ("Marisol", "Quintanilla", "867", "Total Wine", "Market St", self.token):
            assert private not in sent
        assert "Do not identify or describe any person" in sent

    # ---------- cost ----------

    def test_the_same_photo_is_not_read_twice(self, monkeypatch):
        calls = self._model(monkeypatch, [{"ref": "f2", "value": "9", "legible": True}])
        first = self._read().json()
        second = self._read().json()
        other = self._read(image=_jpeg(40)).json()

        assert first == second
        assert first["values"] == [{"fieldId": str(self.cans.id), "value": "9"}]
        assert other["available"] is True
        assert len(calls) == 2  # one per distinct photo

    def test_a_failed_call_is_not_remembered(self, monkeypatch):
        from utils.ai_text import AiUnavailable

        def boom(*_a, **_k):
            raise AiUnavailable("AI service request failed.")

        monkeypatch.setattr("utils.ai_text._post_json", boom)
        assert self._read().json()["available"] is False
        calls = self._model(monkeypatch, [{"ref": "f2", "value": "9", "legible": True}])
        assert self._read().json()["available"] is True
        assert len(calls) == 1

    # ---------- fail open ----------

    def test_slow_model_falls_through_inside_the_limit(self, monkeypatch):
        monkeypatch.setattr(tally_reader, "MODEL_DEADLINE_SECONDS", 0.3)

        def slow(*_a, **_k):
            time.sleep(1.5)
            return _completion({"isTally": True, "readings": []})

        monkeypatch.setattr("utils.ai_text._post_json", slow)
        started = time.monotonic()
        res = self._read()

        assert time.monotonic() - started < 1.2
        assert res.status_code == 200
        assert res.json()["message"] == TALLY_UNREADABLE
        assert res.json()["values"] == []

    def test_real_limits_fit_inside_the_pages_eight_seconds(self):
        assert tally_reader.MODEL_DEADLINE_SECONDS < 8
        assert tally_reader._MODEL_REQUEST_TIMEOUT < tally_reader.MODEL_DEADLINE_SECONDS

    def test_unconfigured_model_falls_through(self, monkeypatch, settings):
        settings.OPENAI_API_KEY = ""

        def boom(*_a, **_k):
            raise AssertionError("no key: the transport must not be reached")

        monkeypatch.setattr("utils.ai_text._post_json", boom)
        res = self._read()

        assert res.status_code == 200
        assert res.json()["available"] is False

    def test_garbage_from_the_model_is_ignored(self, monkeypatch):
        monkeypatch.setattr(
            "utils.ai_text._post_json",
            lambda *a, **k: {"choices": [{"message": {"content": "not json"}}]},
        )
        assert self._read().json()["available"] is False

    def test_no_image_or_an_oversized_one_never_reaches_the_model(self, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("the model must not be called")

        monkeypatch.setattr("utils.ai_text._post_json", boom)
        assert self._read(image="").json()["available"] is False
        big = base64.b64encode(b"x" * 4_100_000).decode("ascii")
        assert self._read(image=big).json()["available"] is False

    # ---------- access and limits ----------

    def test_needs_the_form_session(self, monkeypatch):
        self._model(monkeypatch, [])
        res = self.http.post(
            reverse(
                "events.public_checkin_tally_read",
                kwargs={"code": self.event.walkup_code},
            ),
            data={"session": "", "imageBase64": _jpeg()},
            content_type="application/json",
        )
        assert res.status_code == 401

    def test_rate_limited_per_ambassador_shift(self, monkeypatch):
        calls = self._model(monkeypatch, [])
        statuses = [self._read(image=_jpeg(10 + i)).status_code for i in range(8)]

        assert statuses[:6] == [200] * 6
        assert statuses[6] == 429
        assert self._read().json()["error"] == "rate_limited"
        assert len(calls) == 6

    def test_rate_limited_per_network(self, monkeypatch):
        from events import checkin_views

        monkeypatch.setattr(checkin_views, "TALLY_READ_IP_LIMIT", 2)
        monkeypatch.setattr(checkin_views, "TALLY_READ_SESSION_LIMIT", 99)
        self._model(monkeypatch, [])
        statuses = [self._read(image=_jpeg(10 + i)).status_code for i in range(3)]
        assert statuses == [200, 200, 429]

    def test_reading_saves_nothing_and_submit_still_files(self, monkeypatch):
        from ambassadors import checkin_web
        from recaps.models import (
            CustomFieldValue,
            CustomRecap,
            CustomRecapFile,
            FileRecapCategory,
            FileType,
        )

        FileType.objects.get_or_create(name="image", defaults={"created_by": self.actor})
        FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=self.actor
        )
        monkeypatch.setattr(checkin_web, "_finalize_recap_offthread", lambda recap_id: None)
        self._model(monkeypatch, [{"ref": "f1", "value": "48", "legible": True}])
        assert self._read().json()["available"] is True

        assert CustomRecap.objects.count() == 0
        assert CustomFieldValue.objects.count() == 0
        assert CustomRecapFile.objects.count() == 0

        blob = f"recap_files/checkin/{self.event.uuid}/photo.jpg"
        res = self.http.post(
            reverse(
                "events.public_checkin_recap",
                kwargs={"code": self.event.walkup_code},
            ),
            data={
                "session": self.token,
                "fieldValues": [
                    {"customFieldId": str(self.sampled.id), "value": "48"},
                ],
                "files": [{"blobName": blob}],
                "usedCorpoCard": False,
            },
            content_type="application/json",
        )
        assert res.status_code == 200, res.content
        assert CustomRecap.objects.count() == 1
        # Only the photo the ambassador attached; the tally photo was not kept.
        assert CustomRecapFile.objects.count() == 1
