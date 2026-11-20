"""Voice recap on the public check-in form proposes answers for the whole form.

`POST /checkin/<code>/voice-draft` (a transcript) and
`POST /checkin/<code>/voice-transcribe` (a recording) map what the BA said
onto the fields of their own recap template. These tests go through the real
views and pin that:

* counts, dropdowns, multi-selects, products, yes/no and text all fill;
* an option that is not on the field, a count that is not a number, a low
  confidence answer and an answer the BA never said are all dropped;
* photo and mileage fields are never sent or filled;
* both endpoints are rate limited; recordings are capped by size and length;
* a session for one brand's event can't use another brand's code;
* the recording is not stored; only its size and type are logged.

The OpenAI calls are mocked at ``utils.ai_text._post_json`` (structuring)
and ``utils.ai_text._post_multipart`` (speech-to-text).
"""

from __future__ import annotations

import json

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events import checkin_views
from events.checkin_tokens import make_checkin_session_token
from recaps.voice_draft import (
    interpret_answers,
    match_option,
    parse_number,
    quote_supported,
    voice_targets,
)

TRANSCRIPT = (
    "Pretty busy shift at Total Wine. I sampled about forty people, sold 12 "
    "single cans and 3 four packs. Setup was by the front entrance. Mango "
    "and Lime were the big hits, I poured 25 Mango. Yes, the manager said "
    "we can come back. People said it tastes like fresh fruit, not sweet."
)


def _completion(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


def _ans(ref, value="", choices=None, quote="", confidence="high"):
    return {
        "ref": ref,
        "value": value,
        "choices": choices or [],
        "quote": quote,
        "confidence": confidence,
    }


# ---------------------------------------------------------------- pure parts


def test_parse_number_takes_plain_plausible_numbers_only():
    assert parse_number("40") == "40"
    assert parse_number("1,250") == "1250"
    assert parse_number("forty") is None
    assert parse_number("20-30") is None
    assert parse_number("4.5") is None
    assert parse_number("12.5", money=True) == "12.50"
    assert parse_number("9999999") is None


def test_match_option_is_exact_after_case_and_spacing():
    options = ["Front entrance", "Beer aisle"]
    assert match_option("front  ENTRANCE", options) == "Front entrance"
    assert match_option("entrance", options) is None
    assert match_option("Endcap", options) is None


def test_quote_must_be_in_the_transcript():
    assert quote_supported("sold 12 single cans", TRANSCRIPT)
    assert not quote_supported("sold 80 cases to the store", TRANSCRIPT)
    assert not quote_supported("", TRANSCRIPT)


def test_skips_photo_mileage_and_date_fields():
    targets = voice_targets(
        [
            {"id": "1", "name": "Shelf photo", "type": "image"},
            {"id": "2", "name": "Mileage", "type": "number"},
            {"id": "3", "name": "Event date", "type": "date"},
            {"id": "4", "name": "Notes", "type": "longtext"},
            {"id": "5", "name": "Flavor", "type": "select", "options": []},
        ]
    )
    assert [t["id"] for t in targets] == ["4"]


def test_two_different_answers_for_one_field_are_dropped():
    targets = voice_targets([{"id": "9", "name": "Cans sold", "type": "number"}])
    out = interpret_answers(
        {
            "answers": [
                _ans("f1", "12", quote="sold 12 single cans"),
                _ans("f1", "3", quote="3 four packs"),
            ]
        },
        targets,
        TRANSCRIPT,
    )
    assert out["values"] == []


# ---------------------------------------------------------------- endpoints


@pytest.mark.django_db(transaction=True)
class TestCheckinVoiceRecap(AmbassadorsGraphQLTestCase):
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
        settings.OPENAI_TRANSCRIBE_MODEL = "whisper-1"

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Voice Tenant")
        self.actor = self.create_user(
            username="actor-voice@test.com",
            email="actor-voice@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-voice@test.com",
            email="ba-voice@test.com",
            role=self.roles["ambassador"],
            first_name="Marisol",
            last_name="Quintanilla",
        )
        self.ba = self.create_ambassador(ba_user, phone="555-867-5309")
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Voice template",
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
        self.event.walkup_code = "CK-VOICE1"
        self.event.save(update_fields=["custom_recap_template", "walkup_code"])

        def ftype(name):
            return CustomRecapFieldType.objects.get_or_create(
                name=name, defaults={"created_by": self.actor}
            )[0]

        section = RecapSection.objects.create(
            tenant=self.tenant, name="Results", order=0, created_by=self.actor
        )
        order = iter(range(100))

        def field(name, kind, options=None):
            return CustomField.objects.create(
                name=name,
                custom_recap_template=self.template,
                custom_field_type=ftype(kind),
                recap_section=section,
                order=next(order),
                options=options or [],
                created_by=self.actor,
            )

        self.sampled = field("Total number of consumers sampled", "FormInputInteger")
        self.cans = field("Single cans sold", "number")
        self.packs = field("4-packs sold", "number")
        self.spend = field("Product spend", "number")
        self.setup_spot = field(
            "Where did you set up?", "select", ["Front entrance", "Beer aisle", "Endcap"]
        )
        self.flavors = field(
            "Products sampled", "multiselect", ["Mango", "Lime", "Peach"]
        )
        self.come_back = field("Can we come back?", "select", ["Yes", "No"])
        self.feedback = field("Consumer feedback", "longtext")
        self.notes = field("Account notes", "text")
        self.photo = field("Shelf photo", "image")
        self.mileage = field("Mileage", "number")

        ptype = ProductType.objects.create(
            name="Seltzer", tenant=self.tenant, created_by=self.actor
        )
        self.mango = Product.objects.create(
            name="Mango", product_type=ptype, tenant=self.tenant, created_by=self.actor
        )
        self.http = DjangoClient()
        self.token = make_checkin_session_token(self.event.id, self.ba.id)

    # ---------- helpers ----------

    def _draft(self, transcript=TRANSCRIPT, code=None, token=None):
        return self.http.post(
            reverse(
                "events.public_checkin_voice_draft",
                kwargs={"code": code or self.event.walkup_code},
            ),
            data={"session": token or self.token, "transcript": transcript},
            content_type="application/json",
        )

    def _transcribe(self, audio=b"\x1a\x45\xdf\xa3" + b"0" * 2000, ctype="audio/webm", **extra):
        data = {
            "session": self.token,
            "audio": SimpleUploadedFile("voice.webm", audio, content_type=ctype),
            **extra,
        }
        return self.http.post(
            reverse(
                "events.public_checkin_voice_transcribe",
                kwargs={"code": self.event.walkup_code},
            ),
            data=data,
        )

    @staticmethod
    def _refs(payload) -> dict[str, str]:
        """label → ref, from the prompt the model was sent."""
        text = payload["messages"][1]["content"]
        out = {}
        for line in text.splitlines():
            if line.startswith("- ["):
                ref, rest = line[3:].split("] ", 1)
                label = rest.split(": ", 1)[1].split(" | options", 1)[0]
                label = label.replace(" (dollars, cents allowed)", "")
                if label.startswith("how many of this product were sampled or handed out: "):
                    label = "product:" + label.rsplit(": ", 1)[1]
                out[label] = ref
        return out

    def _model(self, monkeypatch, build):
        """``build(refs)`` returns the model's answers given label → ref."""
        calls = []

        def post(url, payload, headers, timeout):
            calls.append(payload)
            return _completion({"answers": build(self._refs(payload))})

        monkeypatch.setattr("utils.ai_text._post_json", post)
        return calls

    def _full_answers(self, refs):
        return [
            _ans(refs["Total number of consumers sampled"], "40", quote="I sampled about forty people"),
            _ans(refs["Single cans sold"], "12", quote="sold 12 single cans"),
            _ans(refs["4-packs sold"], "3", quote="3 four packs"),
            _ans(refs["Where did you set up?"], "front entrance", quote="Setup was by the front entrance"),
            _ans(refs["Products sampled"], choices=["Mango", "Lime"], quote="Mango and Lime were the big hits"),
            _ans(refs["Can we come back?"], "Yes", quote="Yes, the manager said we can come back"),
            _ans(
                refs["Consumer feedback"],
                "People said it tastes like fresh fruit, not sweet.",
                quote="People said it tastes like fresh fruit, not sweet",
            ),
            _ans(refs["product:Mango"], "25", quote="I poured 25 Mango"),
        ]

    # ---------- tests ----------

    def test_fills_numbers_selects_products_yesno_and_text(self, monkeypatch):
        calls = self._model(monkeypatch, self._full_answers)
        res = self._draft()
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["available"] is True
        values = {v["fieldId"]: v["value"] for v in body["values"]}
        assert values == {
            str(self.sampled.id): "40",
            str(self.cans.id): "12",
            str(self.packs.id): "3",
            str(self.setup_spot.id): "Front entrance",  # canonical option
            str(self.flavors.id): ["Mango", "Lime"],
            str(self.come_back.id): "Yes",
            str(self.feedback.id): "People said it tastes like fresh fruit, not sweet.",
        }
        assert body["productSamples"] == [
            {"productId": str(self.mango.id), "value": "25", "confidence": "high"}
        ]
        assert body["filledCount"] == 8
        # Unmentioned fields are not filled.
        assert str(self.spend.id) not in values
        assert str(self.notes.id) not in values
        # Photo and mileage fields never reach the model.
        prompt = calls[0]["messages"][1]["content"]
        assert "Shelf photo" not in prompt
        assert "Mileage" not in prompt
        assert "Marisol" not in prompt and "555-867-5309" not in prompt

    def test_rejects_invalid_options_numbers_and_invented_answers(self, monkeypatch):
        def build(refs):
            return [
                # Not an option on the field.
                _ans(refs["Where did you set up?"], "Parking lot", quote="Setup was by the front entrance"),
                # One real choice, one invented one: keep the real one only.
                _ans(refs["Products sampled"], choices=["Mango", "Grape"], quote="Mango and Lime were the big hits"),
                # A range is not a count.
                _ans(refs["Single cans sold"], "10-15", quote="sold 12 single cans"),
                # Never said: the quote is not in the transcript.
                _ans(refs["Product spend"], "48.20", quote="I spent forty eight twenty on product"),
                # Low confidence.
                _ans(refs["Account notes"], "Front entrance setup.", quote="Setup was by the front entrance", confidence="low"),
                # Yes/no that is neither.
                _ans(refs["Can we come back?"], "Maybe", quote="the manager said we can come back"),
            ]

        self._model(monkeypatch, build)
        body = self._draft().json()
        values = {v["fieldId"]: v["value"] for v in body["values"]}
        assert values == {str(self.flavors.id): ["Mango"]}
        assert body["productSamples"] == []

    def test_unavailable_without_a_key(self, settings):
        settings.OPENAI_API_KEY = ""
        body = self._draft().json()
        assert body["available"] is False
        assert body["values"] == []

    def test_voice_draft_is_rate_limited_per_session(self, monkeypatch):
        self._model(monkeypatch, lambda refs: [])
        for _ in range(checkin_views.VOICE_DRAFT_SESSION_LIMIT):
            assert self._draft().status_code == 200
        assert self._draft().status_code == 429

    def test_another_brands_code_is_refused(self, monkeypatch):
        calls = self._model(monkeypatch, self._full_answers)
        other_tenant = self.create_tenant(name="Other Brand")
        etype = self.create_event_type("Retail Sampling", other_tenant)
        other = self.create_event(
            name="Elsewhere",
            tenant=other_tenant,
            address="2 Main St",
            event_type=etype,
            date=timezone.now(),
        )
        other.walkup_code = "CK-OTHER1"
        other.save(update_fields=["walkup_code"])
        res = self._draft(code="CK-OTHER1")
        assert res.status_code in (401, 403, 404)
        assert calls == []
        res = self.http.post(
            reverse("events.public_checkin_voice_transcribe", kwargs={"code": "CK-OTHER1"}),
            data={
                "session": self.token,
                "audio": SimpleUploadedFile("v.webm", b"0" * 100, content_type="audio/webm"),
            },
        )
        assert res.status_code in (401, 403, 404)

    def test_transcribes_then_fills_and_keeps_no_audio(self, monkeypatch):
        self._model(monkeypatch, self._full_answers)
        sent = []

        def multipart(url, fields, file, headers, timeout):
            sent.append((url, fields, file[1], file[3], len(file[2])))
            return {"text": TRANSCRIPT}

        monkeypatch.setattr("utils.ai_text._post_multipart", multipart)
        logged = []
        monkeypatch.setattr(
            checkin_views.logger, "info", lambda msg, *args: logged.append(msg % args)
        )
        res = self._transcribe(durationMs="42000")
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["transcribed"] is True
        assert body["transcript"] == TRANSCRIPT
        assert body["draft"]["filledCount"] == 8
        url, fields, filename, ctype, size = sent[0]
        assert url.endswith("/audio/transcriptions")
        assert fields["model"] == "whisper-1"
        assert "Mango" in fields["prompt"]  # product names help spelling
        assert filename == "recording.webm" and ctype == "audio/webm"
        log = " ".join(logged)
        assert "bytes=2004" in log and "type=audio/webm" in log
        assert "Total Wine" not in log  # no transcript text in logs

    def test_ios_mp4_recording_is_accepted(self, monkeypatch):
        self._model(monkeypatch, lambda refs: [])
        names = []
        monkeypatch.setattr(
            "utils.ai_text._post_multipart",
            lambda url, fields, file, headers, timeout: names.append(file[1]) or {"text": "Quiet day."},
        )
        res = self._transcribe(ctype="audio/mp4")
        assert res.status_code == 200
        assert names == ["recording.m4a"]

    def test_transcription_failure_is_a_plain_message(self, monkeypatch):
        from utils.ai_text import AiUnavailable

        def boom(*a, **k):
            raise AiUnavailable("down")

        monkeypatch.setattr("utils.ai_text._post_multipart", boom)
        body = self._transcribe().json()
        assert body["transcribed"] is False
        assert body["draft"] is None
        assert "Type what happened" in body["message"]

    def test_size_length_and_type_caps(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "utils.ai_text._post_multipart",
            lambda *a, **k: called.append(1) or {"text": "x"},
        )
        monkeypatch.setattr(checkin_views, "VOICE_AUDIO_MAX_BYTES", 1000)
        assert self._transcribe(audio=b"0" * 5000).status_code == 413
        assert self._transcribe(audio=b"0" * 500, durationMs="400000").status_code == 413
        assert self._transcribe(audio=b"0" * 500, ctype="text/plain").status_code == 400
        assert self._transcribe(audio=b"", ctype="audio/webm").status_code == 400
        assert called == []

    def test_transcribe_is_rate_limited_per_session(self, monkeypatch):
        self._model(monkeypatch, lambda refs: [])
        monkeypatch.setattr(
            "utils.ai_text._post_multipart", lambda *a, **k: {"text": "Quiet day."}
        )
        for _ in range(checkin_views.VOICE_AUDIO_SESSION_LIMIT):
            assert self._transcribe().status_code == 200
        assert self._transcribe().status_code == 429
