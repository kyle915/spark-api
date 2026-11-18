"""A web check-in recap that claims product spend must carry a receipt photo.

The admin review flagged "spend with no receipt" only after the fact (a Sipli
recap claimed $65 with no receipt image). This pins the submit-time gate:

* spend > 0 with nothing in the receipt bucket is refused, and the refusal
  rolls the whole write back;
* a photo in the receipt bucket clears it; 0 / blank spend needs nothing;
* a brand whose page offers no receipt bucket is never blocked — its BAs
  would have nowhere to put the photo;
* it judges what the recap ENDS UP with, so an edit of a recap that already
  has its receipt goes through without re-sending it;
* the endpoint answers 400 ``needs_receipt`` with the BA-facing sentence.
"""

from __future__ import annotations

import pytest
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps.receipt_gate import RecapNeedsAReceipt

MESSAGE = "Add a photo of your receipt — you entered $65.00 in product spend."


@pytest.mark.django_db(transaction=True)
class TestCheckinRecapNeedsAReceipt(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from recaps.models import (
            CustomField,
            CustomRecapFieldType,
            CustomRecapTemplate,
            FileRecapCategory,
            FileType,
            RecapSection,
        )

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Sipli Receipts")
        self.actor = self.create_user(
            username="actor-rc@test.com",
            email="actor-rc@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-rc@test.com",
            email="ba-rc@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Sipli Retail",
            event_type=etype,
            created_by=self.actor,
        )
        self.event = self.create_event(
            name="Sprouts Main St",
            tenant=self.tenant,
            address="1 Main St",
            event_type=etype,
            date=timezone.now(),
        )
        FileType.objects.get_or_create(name="image", defaults={"created_by": self.actor})
        self.photos_cat = FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=self.actor
        )
        self.receipt_cat = FileRecapCategory.objects.create(
            name="Product Spend", tenant=self.tenant, created_by=self.actor
        )
        self.tenant.checkin_photo_buckets = [
            {"name": "Sampling photos", "min": 1},
            {"name": "Product Spend"},
        ]
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        self.event.refresh_from_db()

        text, _ = CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.actor}
        )
        section = RecapSection.objects.create(
            tenant=self.tenant, name="Spend", order=0, created_by=self.actor
        )
        self.spend_field = CustomField.objects.create(
            name="Product spend amount",
            custom_recap_template=self.template,
            custom_field_type=text,
            recap_section=section,
            order=0,
            required=False,
            created_by=self.actor,
        )
        self.notes_field = CustomField.objects.create(
            name="Account feedback",
            custom_recap_template=self.template,
            custom_field_type=text,
            recap_section=section,
            order=1,
            required=False,
            created_by=self.actor,
        )
        self.http = DjangoClient()

    # -- helpers -----------------------------------------------------------

    def _blob(self, name: str) -> str:
        return f"recap_files/checkin/{self.event.uuid}/{name}.jpg"

    def _photo(self, name="a"):
        return {"blobName": self._blob(name), "category": str(self.photos_cat.id)}

    def _receipt(self, name="r"):
        return {"blobName": self._blob(name), "category": str(self.receipt_cat.id)}

    def _values(self, spend=None, notes=None):
        out = []
        if spend is not None:
            out.append({"customFieldId": str(self.spend_field.id), "value": spend})
        if notes is not None:
            out.append({"customFieldId": str(self.notes_field.id), "value": notes})
        return out

    def _submit(self, files, spend=None, notes=None):
        return checkin_web.submit_checkin_recap(
            event=self.event,
            ambassador=self.ba,
            template=self.template,
            field_values=self._values(spend, notes),
            files=files,
            total_engagements=None,
        )

    def _recaps(self):
        from recaps.models import CustomRecap

        return CustomRecap.objects.filter(event=self.event, ambassador=self.ba)

    # -- the gate ----------------------------------------------------------

    def test_spend_with_no_receipt_photo_is_refused(self):
        with pytest.raises(RecapNeedsAReceipt) as exc:
            self._submit([self._photo()], spend="65.00")
        assert exc.value.message == MESSAGE
        assert self._recaps().count() == 0

    def test_spend_with_a_receipt_photo_files(self):
        recap = self._submit([self._photo(), self._receipt()], spend="65.00")
        assert recap.id
        assert self._recaps().count() == 1

    # (A bare "0" never gets here: the dollars-and-cents guard refuses it.)
    @pytest.mark.parametrize("spend", [None, "", "0.00"])
    def test_no_spend_needs_no_receipt(self, spend):
        recap = self._submit([self._photo()], spend=spend)
        assert recap.id

    def test_a_receipt_dropped_in_the_wrong_bucket_does_not_count(self):
        with pytest.raises(RecapNeedsAReceipt):
            self._submit([self._photo("a"), self._photo("receipt-ish")], spend="65.00")

    def test_brand_without_a_receipt_bucket_is_never_blocked(self):
        """Nowhere to put the photo, so no rule the BA could satisfy."""
        self.tenant.checkin_photo_buckets = None
        self.tenant.save(update_fields=["checkin_photo_buckets"])
        self.event.refresh_from_db()
        recap = self._submit([{"blobName": self._blob("a")}], spend="65.00")
        assert recap.id

    def test_editing_a_recap_that_has_its_receipt_needs_none_re_sent(self):
        first = self._submit([self._photo(), self._receipt()], spend="65.00")
        again = self._submit([], spend="70.00", notes="corrected")
        assert again.id == first.id

    def test_a_refused_edit_keeps_the_filed_answers(self):
        from recaps.models import CustomFieldValue

        recap = self._submit([self._photo()], notes="keep me")
        with pytest.raises(RecapNeedsAReceipt):
            self._submit([], spend="65.00", notes="wiped")
        values = CustomFieldValue.objects.filter(custom_recap=recap)
        assert [v.value for v in values] == ["keep me"]

    # -- the endpoint ------------------------------------------------------

    def _post(self, files, spend):
        from events.checkin_tokens import make_checkin_session_token

        self.event.walkup_code = "SIP-RCPT1"
        self.event.save(update_fields=["walkup_code"])
        token = make_checkin_session_token(self.event.id, self.ba.id)
        return self.http.post(
            reverse("events.public_checkin_recap", kwargs={"code": self.event.walkup_code}),
            data={"session": token, "fieldValues": self._values(spend), "files": files},
            content_type="application/json",
        )

    def test_the_endpoint_answers_400_needs_receipt(self):
        res = self._post([self._photo()], "65.00")
        assert res.status_code == 400
        body = res.json()
        assert body["error"] == "needs_receipt"
        assert body["message"] == MESSAGE
        assert self._recaps().count() == 0

    def test_the_endpoint_files_with_a_receipt(self):
        res = self._post([self._photo(), self._receipt()], "65.00")
        assert res.status_code == 200, res.content
        assert res.json()["success"] is True
