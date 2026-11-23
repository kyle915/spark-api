"""Famus clerk sign-off is required when the template asks for it.

Clock-out does not go through ``submit_checkin_recap``. A template without
the three sign-off fields files with no signature, so other brands are
unchanged.
"""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from django.utils import timezone
from PIL import Image

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps.models import CustomField, CustomFieldValue, CustomRecapFieldType, FileType, RecapSection
from recaps.signoff import (
    FIRST_NAME_MESSAGE,
    LAST_INITIAL_MESSAGE,
    SIGNATURE_MISSING_MESSAGE,
    SignoffIncomplete,
)


def _png() -> str:
    image = Image.new("L", (80, 40), 255)
    for x in range(8, 72):
        image.putpixel((x, 18), 15)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@pytest.mark.django_db(transaction=True)
class TestCheckinSignoff(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Famus Energy", slug="famus-energy")
        self.actor = self.create_user(
            username="actor-so@test.com",
            email="actor-so@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-so@test.com",
            email="ba-so@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)
        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = self._template(etype)
        self.event = self.create_event(
            name="Publix",
            tenant=self.tenant,
            address="1 Main St",
            event_type=etype,
            date=timezone.now(),
        )
        FileType.objects.get_or_create(name="image", defaults={"created_by": self.actor})
        section = RecapSection.objects.create(
            tenant=self.tenant,
            name="Store sign-off",
            order=4,
            created_by=self.actor,
        )
        self.first = self._field(section, "First name", "text", 0)
        self.initial = self._field(section, "Last initial", "letter", 1)
        self.signature = self._field(section, "Signature", "signature", 2)

    def _template(self, etype):
        from recaps.models import CustomRecapTemplate

        return CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="FAMUS Energy · Retail Sampling Recap",
            event_type=etype,
            created_by=self.actor,
        )

    def _field(self, section, name, kind, order):
        field_type, _ = CustomRecapFieldType.objects.get_or_create(
            name=kind, defaults={"created_by": self.actor}
        )
        return CustomField.objects.create(
            name=name,
            custom_recap_template=self.template,
            custom_field_type=field_type,
            recap_section=section,
            order=order,
            required=True,
            created_by=self.actor,
        )

    def _photo(self):
        return [{"blobName": f"recap_files/checkin/{self.event.uuid}/shot.jpg"}]

    def _values(self, **overrides):
        base = {
            str(self.first.id): "Alex",
            str(self.initial.id): "m",
            str(self.signature.id): _png(),
        }
        base.update(overrides)
        return [{"customFieldId": key, "value": value} for key, value in base.items()]

    def _submit(self, field_values):
        return checkin_web.submit_checkin_recap(
            event=self.event,
            ambassador=self.ba,
            template=self.template,
            field_values=field_values,
            files=self._photo(),
            total_engagements=None,
        )

    def test_a_signed_recap_stores_the_name_initial_and_png(self):
        recap = self._submit(self._values())
        stored = {
            row.custom_field_id: row.value
            for row in CustomFieldValue.objects.filter(custom_recap=recap)
        }
        assert stored[self.first.id] == "Alex"
        assert stored[self.initial.id] == "M"
        assert stored[self.signature.id].startswith("data:image/png;base64,")

    def test_missing_signature_rolls_the_recap_back(self):
        from recaps.models import CustomRecap

        with pytest.raises(SignoffIncomplete, match=SIGNATURE_MISSING_MESSAGE):
            self._submit(self._values(**{str(self.signature.id): ""}))
        assert not CustomRecap.objects.filter(event=self.event, ambassador=self.ba).exists()

    def test_a_bad_initial_is_refused(self):
        with pytest.raises(SignoffIncomplete, match=LAST_INITIAL_MESSAGE):
            self._submit(self._values(**{str(self.initial.id): "Mo"}))

    def test_a_numbered_first_name_is_refused(self):
        with pytest.raises(SignoffIncomplete, match=FIRST_NAME_MESSAGE):
            self._submit(self._values(**{str(self.first.id): "Alex2"}))

    def test_a_template_without_signoff_still_files(self):
        CustomField.objects.filter(custom_recap_template=self.template).delete()
        recap = self._submit([])
        assert recap.pk
        assert not CustomFieldValue.objects.filter(custom_recap=recap).exists()
