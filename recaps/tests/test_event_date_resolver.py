"""
Coverage for the recap "Event Date" read-resilience fix.

Background (incident: internally-created recaps showing "N/A" for Event Date):
    The recap PDF + custom-recap info panel originally read ONLY ``Event.date``
    with no fallback. Events materialized BEFORE commit 4b2d269 (#718, which
    started copying ``request.date`` into ``Event.date``) have
    ``Event.date IS NULL`` but ``Event.start_time`` populated (and the date
    lives on the parent Request too) — so those recaps showed "N/A"/"-".

    The fix makes the read side resilient: the ``event_date`` GraphQL resolver
    (recaps/types.py) now falls back
        event.date → event.start_time → request.date → request.start_time
    The fallback lives in the module-level pure helper ``_resolve_event_date``
    so it can be exercised here without standing up the Strawberry schema.

These tests pin BOTH the pure fallback chain (every branch, no DB) and a
DB-backed Event whose ``date`` is null but ``start_time`` is set — the exact
pre-#718 shape the bug produced.
"""

from __future__ import annotations

from datetime import datetime, timezone as _tz
from types import SimpleNamespace

import pytest

from recaps.types import _resolve_event_date


def _aware(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=_tz.utc)


# ─── Pure helper: the fallback chain, no DB ──────────────────────────────


class TestResolveEventDatePure:
    def test_prefers_event_date_when_present(self):
        ev = SimpleNamespace(
            date=_aware(2026, 1, 2),
            start_time=_aware(2026, 3, 4),
            request=SimpleNamespace(
                date=_aware(2026, 5, 6), start_time=_aware(2026, 7, 8)
            ),
        )
        assert _resolve_event_date(ev) == _aware(2026, 1, 2)

    def test_falls_back_to_start_time_when_date_null(self):
        # The exact pre-#718 shape: date is null, start_time is set.
        ev = SimpleNamespace(
            date=None,
            start_time=_aware(2026, 3, 4),
            request=SimpleNamespace(
                date=_aware(2026, 5, 6), start_time=_aware(2026, 7, 8)
            ),
        )
        assert _resolve_event_date(ev) == _aware(2026, 3, 4)

    def test_falls_back_to_request_date_when_event_has_neither(self):
        ev = SimpleNamespace(
            date=None,
            start_time=None,
            request=SimpleNamespace(
                date=_aware(2026, 5, 6), start_time=_aware(2026, 7, 8)
            ),
        )
        assert _resolve_event_date(ev) == _aware(2026, 5, 6)

    def test_falls_back_to_request_start_time_last(self):
        ev = SimpleNamespace(
            date=None,
            start_time=None,
            request=SimpleNamespace(date=None, start_time=_aware(2026, 7, 8)),
        )
        assert _resolve_event_date(ev) == _aware(2026, 7, 8)

    def test_returns_none_when_everything_absent(self):
        ev = SimpleNamespace(
            date=None,
            start_time=None,
            request=SimpleNamespace(date=None, start_time=None),
        )
        assert _resolve_event_date(ev) is None

    def test_null_safe_no_event(self):
        assert _resolve_event_date(None) is None

    def test_null_safe_no_request(self):
        ev = SimpleNamespace(date=None, start_time=None, request=None)
        assert _resolve_event_date(ev) is None


# ─── DB-backed: a real Event with null date + start_time set ─────────────


@pytest.mark.django_db
class TestEventDateResolverDb:
    """The resolver path on a real, pre-#718-shaped Event: date null,
    start_time set. The resolver isoformat()s the derived value — so we assert
    the helper, applied to the fetched Event, yields start_time (and the
    request-date fallback when start_time is also null)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from tenants.tests.base import BaseGraphQLTestCase

        helper = BaseGraphQLTestCase()
        self.tenant = helper.create_tenant(name="Liquid Death", slug="liquid-death")
        from events import models as event_models

        self.event_models = event_models
        system_user = helper.get_system_user()
        self.system_user = system_user
        self.req_status = event_models.RequestStatus.objects.create(
            name="Approved", slug="approved", tenant=self.tenant,
            created_by=system_user,
        )
        self.ev_status = event_models.EventStatus.objects.create(
            name="Approved", slug="approved", tenant=self.tenant,
            created_by=system_user,
        )
        self.request_type = event_models.RequestType.objects.create(
            name="Sampling", tenant=self.tenant, created_by=system_user,
        )

    def _make_event(self, *, date, start_time, req_date=None, req_start=None):
        request = self.event_models.Request.objects.create(
            name="Vons",
            address="1608 Broadway St",
            tenant=self.tenant,
            status=self.req_status,
            request_type=self.request_type,
            date=req_date,
            start_time=req_start,
            created_by=self.system_user,
        )
        return self.event_models.Event.objects.create(
            name="Vons activation",
            tenant=self.tenant,
            request=request,
            status=self.ev_status,
            date=date,
            start_time=start_time,
            created_by=self.system_user,
        )

    def test_null_date_uses_start_time(self):
        ev = self._make_event(date=None, start_time=_aware(2026, 5, 29, 12))
        ev = self.event_models.Event.objects.select_related("request").get(id=ev.id)
        derived = _resolve_event_date(ev)
        assert derived == _aware(2026, 5, 29, 12)
        # The resolver returns derived.isoformat().
        assert derived.isoformat() == _aware(2026, 5, 29, 12).isoformat()

    def test_null_date_and_start_time_uses_request_date(self):
        ev = self._make_event(
            date=None,
            start_time=None,
            req_date=_aware(2026, 5, 29, 9),
            req_start=_aware(2026, 5, 29, 9),
        )
        ev = self.event_models.Event.objects.select_related("request").get(id=ev.id)
        assert _resolve_event_date(ev) == _aware(2026, 5, 29, 9)
