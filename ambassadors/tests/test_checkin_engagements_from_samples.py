"""File recap copies Spark engagements from the samples-given question.

Torch (and most retail templates) already ask "Total number of consumers
sampled". That number IS CustomRecap.total_engagements. The File recap page
used to show both, so BAs typed the same count twice. The page now hides the
native box; this pins that the write path still fills the column from the
template answer so the recap PDF / KPI don't go blank.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestCheckinEngagementsFromSamplesGiven(AmbassadorsGraphQLTestCase):
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
        self.tenant = self.create_tenant(name="Torch Engagements Map")
        self.actor = self.create_user(
            username="actor-eng@test.com",
            email="actor-eng@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-eng@test.com",
            email="ba-eng@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)

        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Torch THC-Retail Sampling",
            event_type=etype,
            created_by=self.actor,
        )
        self.event = self.create_event(
            name="Total Wine Lees Summit",
            tenant=self.tenant,
            address="1648 NW Chipman Road",
            event_type=etype,
            date=timezone.now(),
        )
        FileType.objects.get_or_create(
            name="image", defaults={"created_by": self.actor}
        )
        FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=self.actor
        )
        field_type, _ = CustomRecapFieldType.objects.get_or_create(
            name="number", defaults={"created_by": self.actor}
        )
        section = RecapSection.objects.create(
            tenant=self.tenant,
            name="Consumer Engagement",
            order=0,
            created_by=self.actor,
        )
        self.sampled_field = CustomField.objects.create(
            name="Total number of consumers sampled",
            custom_recap_template=self.template,
            custom_field_type=field_type,
            recap_section=section,
            order=0,
            required=True,
            created_by=self.actor,
        )
        self.first_time_field = CustomField.objects.create(
            name="First time consumers?",
            custom_recap_template=self.template,
            custom_field_type=field_type,
            recap_section=section,
            order=1,
            required=False,
            created_by=self.actor,
        )

    def _blob(self) -> str:
        return f"recap_files/checkin/{self.event.uuid}/a.jpg"

    def _submit(self, field_values, total_engagements=None):
        return checkin_web.submit_checkin_recap(
            event=self.event,
            ambassador=self.ba,
            template=self.template,
            field_values=field_values,
            files=[{"blobName": self._blob()}],
            total_engagements=total_engagements,
        )

    def test_samples_given_fills_total_engagements(self):
        recap = self._submit(
            [
                {"customFieldId": str(self.sampled_field.id), "value": "87"},
                {"customFieldId": str(self.first_time_field.id), "value": "61"},
            ]
        )
        recap.refresh_from_db()
        assert recap.total_engagements == 87

    def test_samples_given_wins_over_a_duplicate_engagements_box(self):
        recap = self._submit(
            [{"customFieldId": str(self.sampled_field.id), "value": "87"}],
            total_engagements=120,
        )
        recap.refresh_from_db()
        assert recap.total_engagements == 87

    def test_demographics_prose_is_not_treated_as_engagements(self):
        from recaps.models import CustomField, RecapSection

        demo = CustomField.objects.create(
            name="General demographics of consumers sampled (age range)",
            custom_recap_template=self.template,
            custom_field_type=self.sampled_field.custom_field_type,
            recap_section=RecapSection.objects.get(
                tenant=self.tenant, name="Consumer Engagement"
            ),
            order=2,
            required=False,
            created_by=self.actor,
        )
        recap = self._submit(
            [
                {"customFieldId": str(demo.id), "value": "ranged from 19 to 60s"},
            ],
            total_engagements=40,
        )
        recap.refresh_from_db()
        assert recap.total_engagements == 40
