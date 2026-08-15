"""Check-in File recap must not convert HEIC inline.

iPhone photos land as .heic. Safari cannot canvas-decode them, so the
client uploads the original. Converting those blobs inside
``submit_checkin_recap`` (``ensure_jpg_sibling_blob``) blocks the File
recap request — eight shots on LTE is enough to time out.

Conversion is scheduled after ``transaction.on_commit`` via the same
Cloud Tasks path other recap uploads use. JPG siblings appear shortly
after; the submit itself returns fast.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestCheckinRecapHeicIsAsync(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from recaps.models import (
            CustomRecapFieldType,
            CustomRecapTemplate,
            FileRecapCategory,
            FileType,
        )

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="HEIC Floor")
        self.actor = self.create_user(
            username="actor-heic@test.com",
            email="actor-heic@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-heic@test.com",
            email="ba-heic@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)

        etype = self.create_event_type("Retail Sampling", self.tenant)
        self.template = CustomRecapTemplate.objects.create(
            tenant=self.tenant,
            name="Retail Sampling",
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
        FileRecapCategory.objects.create(
            name="Sampling photos", tenant=self.tenant, created_by=self.actor
        )
        CustomRecapFieldType.objects.get_or_create(
            name="text", defaults={"created_by": self.actor}
        )

    def _submit(self, files):
        return checkin_web.submit_checkin_recap(
            event=self.event,
            ambassador=self.ba,
            template=self.template,
            field_values=[],
            files=files,
            total_engagements=None,
        )

    def _blob(self, name: str) -> str:
        return f"recap_files/checkin/{self.event.uuid}/{name}"

    def test_heic_submit_schedules_convert_and_does_not_run_it_inline(self):
        heic = self._blob("IMG_1234.heic")
        with (
            patch("recaps.heic_conversion.ensure_jpg_sibling_blob") as convert,
            patch("recaps.heic_conversion.schedule_jpg_sibling_blob") as schedule,
        ):
            recap = self._submit([{"blobName": heic}])

        convert.assert_not_called()
        schedule.assert_called_once_with(heic)
        from recaps.models import CustomRecapFile

        assert CustomRecapFile.objects.filter(custom_recap=recap).count() == 1

    def test_eight_heics_schedule_each_and_never_convert_inline(self):
        blobs = [self._blob(f"IMG_{i}.HEIC") for i in range(8)]
        with (
            patch("recaps.heic_conversion.ensure_jpg_sibling_blob") as convert,
            patch("recaps.heic_conversion.schedule_jpg_sibling_blob") as schedule,
        ):
            recap = self._submit([{"blobName": b} for b in blobs])

        convert.assert_not_called()
        assert schedule.call_count == 8
        assert [c.args[0] for c in schedule.call_args_list] == blobs
        from recaps.models import CustomRecapFile

        assert CustomRecapFile.objects.filter(custom_recap=recap).count() == 8

    def test_jpeg_submit_does_not_schedule_heic_convert(self):
        jpeg = self._blob("table.jpg")
        with (
            patch("recaps.heic_conversion.ensure_jpg_sibling_blob") as convert,
            patch("recaps.heic_conversion.schedule_jpg_sibling_blob") as schedule,
        ):
            self._submit([{"blobName": jpeg}])

        convert.assert_not_called()
        schedule.assert_not_called()
