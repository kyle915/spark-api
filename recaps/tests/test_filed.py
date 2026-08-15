"""Filed = submitted content, not "a Recap row exists."

Clock-out inserts an empty stub. That row must not satisfy has_recap,
must not increment recapsFiledCount, and must not hide the event from
missing_recap_events.
"""

import pytest
from django.utils import timezone

from ambassadors.models import FileType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from recaps import models as rm
from recaps.filed import (
    custom_filed_q,
    events_missing_filed_recap,
    has_filed_recap,
    legacy_filed_q,
)


@pytest.mark.django_db(transaction=True)
class TestFiledContent(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Filed Tenant")
        self.event = self.create_event(name="Stub shift", tenant=self.tenant)
        ba_user = self.create_user(
            username="filed-ba",
            email="filed-ba@example.com",
            role=self.roles["ambassador"],
            first_name="Nia",
            last_name="Holt",
        )
        self.ambassador = self.create_ambassador(ba_user)
        self.file_type = FileType.objects.create(
            name="image", created_by=self.sys
        )

    def _legacy(self, **kwargs):
        defaults = dict(
            name="legacy",
            event=self.event,
            ambassador=self.ambassador,
            created_by=self.sys,
            updated_by=self.sys,
        )
        defaults.update(kwargs)
        return rm.Recap.objects.create(**defaults)

    def _custom(self, **kwargs):
        et = self.create_event_type("Sampling", self.tenant)
        tpl = rm.CustomRecapTemplate.objects.create(
            name="tpl",
            event_type=et,
            tenant=self.tenant,
            created_by=self.sys,
        )
        defaults = dict(
            name="custom",
            event=self.event,
            ambassador=self.ambassador,
            tenant=self.tenant,
            custom_recap_template=tpl,
            created_by=self.sys,
            updated_by=self.sys,
        )
        defaults.update(kwargs)
        return rm.CustomRecap.objects.create(**defaults)

    def test_empty_clock_out_stub_is_not_filed(self):
        stub = self._legacy()
        assert not rm.Recap.objects.filter(legacy_filed_q()).filter(id=stub.id).exists()
        assert not has_filed_recap(
            ambassador_id=self.ambassador.id, event_id=self.event.id
        )

    def test_submitted_at_counts_as_filed(self):
        self._legacy(submited_at=timezone.now())
        assert has_filed_recap(
            ambassador_id=self.ambassador.id, event_id=self.event.id
        )

    def test_metrics_count_as_filed(self):
        self._legacy(products_sold=6)
        assert has_filed_recap(
            ambassador_id=self.ambassador.id, event_id=self.event.id
        )

    def test_zero_metric_counts_as_filed(self):
        self._legacy(total_engagements=0)
        assert has_filed_recap(
            ambassador_id=self.ambassador.id, event_id=self.event.id
        )

    def test_photo_counts_as_filed(self):
        recap = self._legacy()
        rm.RecapFile.objects.create(
            name="hero.jpg",
            file="recap_files/hero.jpg",
            file_type=self.file_type,
            recap=recap,
            created_by=self.sys,
        )
        assert has_filed_recap(
            ambassador_id=self.ambassador.id, event_id=self.event.id
        )

    def test_custom_stub_is_not_filed(self):
        stub = self._custom()
        assert not rm.CustomRecap.objects.filter(custom_filed_q()).filter(
            id=stub.id
        ).exists()
        assert not has_filed_recap(
            ambassador_id=self.ambassador.id, event_id=self.event.id
        )

    def test_custom_submitted_at_counts(self):
        self._custom(submitted_at=timezone.now())
        assert has_filed_recap(
            ambassador_id=self.ambassador.id, event_id=self.event.id
        )

    def test_empty_stub_event_is_still_missing(self):
        self._legacy()
        qs = events_missing_filed_recap(
            self.event.__class__.objects.filter(id=self.event.id)
        )
        assert list(qs.values_list("id", flat=True)) == [self.event.id]

    def test_filed_event_is_not_missing(self):
        self._legacy(products_sold=3)
        qs = events_missing_filed_recap(
            self.event.__class__.objects.filter(id=self.event.id)
        )
        assert list(qs.values_list("id", flat=True)) == []
