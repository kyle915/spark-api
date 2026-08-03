"""A filed check-in recap must carry at least one photo.

The page has always refused to submit without one ("Add at least one photo of
your event."), but that was the ONLY thing enforcing it: the recap endpoint read
``files`` straight off the request body, validated no more than
``isinstance(files, list)``, and would happily file a recap with none — an empty
row in the client's report, indistinguishable from a real shift in the KPIs.

What these tests pin, because each one is a way this could go quietly wrong:

* the hole itself — ``files: []`` is refused, and refused at the API, not just
  in a browser that a curl can skip;
* refusal is a ROLLBACK. The write path deletes and rewrites an existing
  recap's field values in the same transaction, so a refused edit that left
  those deleted would destroy a BA's filed answers to enforce a photo rule;
* it counts what the recap ENDS UP with, not what the request carried. A forged
  request whose blobs are all out-of-scope leaves no photo behind while looking
  non-empty, and that is the case worth catching;
* an edit of a recap that ALREADY has photos still goes through. That shift has
  photos; it is not the thing being blocked. The page can't currently reach this
  (its ``photos`` state starts empty, so a BA must re-add a shot to edit) — which
  is exactly why the API must not be what stands in the way when that is fixed.
"""

from __future__ import annotations

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestCheckinRecapNeedsAPhoto(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from recaps.models import (
            CustomRecapFieldType,
            CustomRecapTemplate,
            FileRecapCategory,
            FileType,
        )

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Liquid Death Photo Floor")
        self.actor = self.create_user(
            username="actor-np@test.com",
            email="actor-np@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-np@test.com",
            email="ba-np@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)

        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="LD-Retail Sampling",
            event_type=etype,
            created_by=self.actor,
        )
        self.event = self.create_event(
            name="HEB Congress",
            tenant=self.tenant,
            address="123 Congress Ave",
            event_type=etype,
            date=timezone.now(),
        )
        FileType.objects.get_or_create(
            name="image", defaults={"created_by": self.actor}
        )
        self.photos_cat = FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=self.actor
        )
        self.field_type, _ = CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.actor}
        )
        # The endpoint tests below go through the real URLconf, so they need a
        # request client of their own — the shared base class is a plain helper
        # class, not a Django TestCase.
        self.http = DjangoClient()

    # -- helpers -----------------------------------------------------------

    def _submit(self, files, field_values=None):
        return checkin_web.submit_checkin_recap(
            event=self.event,
            ambassador=self.ba,
            template=self.template,
            field_values=field_values or [],
            files=files,
            total_engagements=None,
        )

    def _blob(self, name: str) -> str:
        return f"recap_files/checkin/{self.event.uuid}/{name}.jpg"

    def _recaps(self):
        from recaps.models import CustomRecap

        return CustomRecap.objects.filter(event=self.event, ambassador=self.ba)

    def _photo_count(self, recap) -> int:
        from recaps.models import CustomRecapFile

        return CustomRecapFile.objects.filter(custom_recap=recap).count()

    def _a_field(self):
        """One text field on the template, to prove a refused edit doesn't take
        the BA's existing answers down with it. Must hang off this template —
        the write path only accepts a field whose ``custom_recap_template`` is
        the one being submitted against."""
        from recaps.models import CustomField, RecapSection

        section = RecapSection.objects.create(
            tenant=self.tenant, name="Notes", order=0, created_by=self.actor
        )
        return CustomField.objects.create(
            name="Account Feedback",
            custom_recap_template=self.template,
            custom_field_type=self.field_type,
            recap_section=section,
            order=0,
            required=False,
            created_by=self.actor,
        )

    # -- the hole ----------------------------------------------------------

    def test_a_recap_with_no_files_is_refused(self):
        with pytest.raises(checkin_web.RecapNeedsAPhoto):
            self._submit([])

    def test_refusing_leaves_no_recap_behind(self):
        """Not a half-filed row: the whole submission rolls back."""
        with pytest.raises(checkin_web.RecapNeedsAPhoto):
            self._submit([])
        assert self._recaps().count() == 0

    def test_files_whose_blobs_are_all_out_of_scope_is_refused(self):
        """The adversarial shape: the request looks non-empty, but every blob is
        dropped for sitting outside this session's own prefix, so the recap would
        end up with nothing. Counting the REQUEST would wave this through."""
        with pytest.raises(checkin_web.RecapNeedsAPhoto):
            self._submit(
                [
                    {"blobName": "recap_files/checkin/some-other-event/a.jpg"},
                    {"blobName": "../../etc/passwd"},
                ]
            )
        assert self._recaps().count() == 0

    def test_unusable_file_entries_are_refused(self):
        """Entries carrying no resolvable blob name at all."""
        with pytest.raises(checkin_web.RecapNeedsAPhoto):
            self._submit([{}, {"blobName": ""}, {"category": "1"}])
        assert self._recaps().count() == 0

    # -- what must keep working -------------------------------------------

    def test_one_photo_is_enough(self):
        recap = self._submit([{"blobName": self._blob("a")}])
        assert self._photo_count(recap) == 1
        assert self._recaps().count() == 1

    def test_a_mix_of_good_and_rejected_blobs_still_files(self):
        """One survivor clears the floor — a BA whose other shots failed is not
        the person this rule is aimed at."""
        recap = self._submit(
            [
                {"blobName": "recap_files/checkin/elsewhere/bad.jpg"},
                {"blobName": self._blob("good")},
            ]
        )
        assert self._photo_count(recap) == 1

    def test_editing_a_recap_that_already_has_photos_needs_no_new_ones(self):
        """The recap is what must have a photo, not every request touching it."""
        field = self._a_field()
        recap = self._submit(
            [{"blobName": self._blob("a")}],
            field_values=[{"customFieldId": str(field.id), "value": "first pass"}],
        )

        again = self._submit(
            [], field_values=[{"customFieldId": str(field.id), "value": "corrected"}]
        )

        assert again.id == recap.id
        assert self._photo_count(again) == 1
        assert self._recaps().count() == 1
        from recaps.models import CustomFieldValue

        values = CustomFieldValue.objects.filter(custom_recap=again)
        assert [v.value for v in values] == ["corrected"]

    def test_a_refused_edit_does_not_destroy_the_existing_field_values(self):
        """The write path deletes field values before rewriting them, inside the
        same transaction this raises in. If the rollback didn't hold, enforcing a
        photo rule would silently eat a BA's filed answers."""
        field = self._a_field()
        recap = self._submit(
            [{"blobName": self._blob("a")}],
            field_values=[{"customFieldId": str(field.id), "value": "keep me"}],
        )
        # Strip its photos so the recap now sits below the floor, then edit it.
        from recaps.models import CustomFieldValue, CustomRecapFile

        CustomRecapFile.objects.filter(custom_recap=recap).delete()

        with pytest.raises(checkin_web.RecapNeedsAPhoto):
            self._submit(
                [], field_values=[{"customFieldId": str(field.id), "value": "wiped"}]
            )

        values = CustomFieldValue.objects.filter(custom_recap=recap)
        assert [v.value for v in values] == ["keep me"]

    # -- the endpoint ------------------------------------------------------

    def test_the_endpoint_answers_400_with_the_page_s_own_sentence(self):
        """A curl skipping the browser check gets a refusal it can show a BA —
        not the generic 500 every other failure in this view returns."""
        from events.checkin_tokens import make_checkin_session_token

        self.event.walkup_code = "LD-NOPHOTO"
        self.event.save(update_fields=["walkup_code"])
        token = make_checkin_session_token(self.event.id, self.ba.id)

        res = self.http.post(
            reverse(
                "events.public_checkin_recap", kwargs={"code": self.event.walkup_code}
            ),
            data={"session": token, "fieldValues": [], "files": []},
            content_type="application/json",
        )

        assert res.status_code == 400
        body = res.json()
        assert body["error"] == "needs_photo"
        assert body["message"] == "Add at least one photo of your event."
        assert self._recaps().count() == 0

    def test_the_endpoint_still_accepts_a_recap_with_a_photo(self):
        from events.checkin_tokens import make_checkin_session_token

        self.event.walkup_code = "LD-OKPHOTO"
        self.event.save(update_fields=["walkup_code"])
        token = make_checkin_session_token(self.event.id, self.ba.id)

        res = self.http.post(
            reverse(
                "events.public_checkin_recap", kwargs={"code": self.event.walkup_code}
            ),
            data={
                "session": token,
                "fieldValues": [],
                "files": [{"blobName": self._blob("a")}],
            },
            content_type="application/json",
        )

        assert res.status_code == 200
        assert res.json()["success"] is True
        assert self._recaps().count() == 1
