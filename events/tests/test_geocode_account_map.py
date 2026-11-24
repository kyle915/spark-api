"""``geocode_account_map``: positions for requests the Account Map cannot draw.

* Dry run by default: counts, no Google call, nothing stored.
* ``--apply`` looks up each distinct address once, stores ``RequestMapPoint``,
  reuses answers already held for the same address, and skips requests that
  already have a position (their own, or an event's).
* Soft-deleted requests and other tenants (with ``--tenant``) are left alone.

Also the store-label rules shared with the front end.
"""

from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command
from django.utils import timezone

from events import models as em
from events.map_points import address_hash
from events.store_labels import clean_store_label, is_date_only
from events.tests.base import EventsGraphQLTestCase

LOOKUP = "events.management.commands.geocode_account_map.geocoding.lookup_address"
STATUS = "events.management.commands.geocode_account_map.geocoding.geocoding_status"

TEMPE = "1730 E Warner Rd, Tempe, AZ 85284"
PHOENIX = "1670 W Bethany Home Rd, Phoenix, AZ 85015"


@pytest.mark.parametrize(
    "raw, cleaned",
    [
        ("BP · 2027-01-11", "BP"),
        ("FOOD GARDEN FRAN-05/17/2026", "FOOD GARDEN FRAN"),
        ("LINCOLN MARKET MAN -BF 12 - 2026-06-06", "LINCOLN MARKET MAN -BF 12"),
        ("Total Wine (May 17, 2026)", "Total Wine"),
        ("Sprouts 10am-2pm 5/3", "Sprouts"),
        ("H-E-B Demo · Sat 11/12", "H-E-B Demo"),
        ("7-Eleven #123", "7-Eleven #123"),
        ("7-11", "7-11"),
        ("Kroger 123", "Kroger 123"),
        ("05/17/2026", "05/17/2026"),
        ("Sunoco 5/3", "Sunoco"),
        ("Marathon 12", "Marathon 12"),
        ("Mayfair Market 7", "Mayfair Market 7"),
        ("Saturday, May 17 Kroger", "Kroger"),
        ("", ""),
    ],
)
def test_clean_store_label(raw, cleaned):
    assert clean_store_label(raw) == cleaned


def test_is_date_only():
    assert is_date_only("05/17/2026")
    assert is_date_only("2026-05-23")
    assert not is_date_only("BP · 2027-01-11")
    assert not is_date_only("7-11")
    assert not is_date_only("")


@pytest.mark.django_db(transaction=True)
class TestGeocodeAccountMap(EventsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Geo Tenant")
        self.other = self.create_tenant(name="Other Geo Tenant")
        self.rt = em.RequestType.objects.create(
            name="Retail Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.status = em.RequestStatus.objects.create(
            name="Approved", tenant=self.tenant, created_by=self.sys
        )

    def _request(self, address, *, tenant=None, coords=None):
        return em.Request.objects.create(
            name="visit",
            address=address,
            request_type=self.rt,
            tenant=tenant or self.tenant,
            created_by=self.sys,
            status=self.status,
            date=timezone.now(),
            coordinates=coords if coords is not None else [],
        )

    def _run(self, **opts):
        out = StringIO()
        with mock.patch(STATUS, return_value="geocoding: test"):
            call_command("geocode_account_map", stdout=out, sleep=0, **opts)
        return out.getvalue()

    def test_dry_run_counts_and_never_calls_google_or_stores(self):
        self._request(TEMPE)
        self._request(TEMPE)
        self._request(PHOENIX)
        self._request("TBD")
        self._request(PHOENIX, coords=[33.5, -112.0])
        with mock.patch(LOOKUP) as lookup:
            out = self._run()
        lookup.assert_not_called()
        assert em.RequestMapPoint.objects.count() == 0
        assert "Without a position: 3 request(s)" in out
        assert "(2 distinct address(es))" in out
        assert "1 have no street address" in out
        assert "1 have their own position" in out

    def test_apply_looks_up_each_address_once_and_stores_points(self):
        a = self._request(TEMPE)
        b = self._request(TEMPE)
        c = self._request(PHOENIX)
        answers = {
            TEMPE: ("ok", (33.334, -111.912), ""),
            PHOENIX: ("not_found", None, "ZERO_RESULTS"),
        }
        with mock.patch(LOOKUP, side_effect=lambda addr: answers[addr]) as lookup:
            self._run(apply=True)
        assert lookup.call_count == 2
        points = {p.request_id: p for p in em.RequestMapPoint.objects.all()}
        assert points[a.pk].status == "ok" and points[a.pk].lat == 33.334
        assert points[b.pk].status == "ok"
        assert points[c.pk].status == "not_found" and points[c.pk].lat is None
        assert points[a.pk].address_hash == address_hash(TEMPE)

        # A second run has nothing left to do.
        with mock.patch(LOOKUP) as again:
            self._run(apply=True)
        again.assert_not_called()

    def test_reuses_an_events_answer_for_the_same_address(self):
        req = self._request(TEMPE)
        other_req = self._request(TEMPE, coords=[])
        ev = self.create_event(
            name="shift", tenant=self.tenant, request=other_req, date=timezone.now(), address=TEMPE
        )
        em.EventMapPoint.objects.create(
            event=ev, lat=33.3, lng=-111.9, address_hash=address_hash(TEMPE), status="ok"
        )
        with mock.patch(LOOKUP) as lookup:
            self._run(apply=True)
        lookup.assert_not_called()
        point = em.RequestMapPoint.objects.get(request=req)
        assert (point.lat, point.lng) == (33.3, -111.9)
        # The request already placed by its event got no point of its own.
        assert not em.RequestMapPoint.objects.filter(request=other_req).exists()

    def test_skips_deleted_requests_and_other_tenants(self):
        gone = self._request(TEMPE)
        em.Request.objects.filter(pk=gone.pk).update(deleted_at=timezone.now())
        theirs = self._request(PHOENIX, tenant=self.other)
        mine = self._request(PHOENIX)
        with mock.patch(LOOKUP, return_value=("ok", (33.52, -112.09), "")):
            self._run(apply=True, tenant=self.tenant.id)
        assert set(em.RequestMapPoint.objects.values_list("request_id", flat=True)) == {
            mine.pk
        }
        assert theirs.pk != mine.pk

    def test_no_key_stops_the_run_and_stores_nothing(self):
        self._request(TEMPE)
        self._request(PHOENIX)
        with mock.patch(LOOKUP, return_value=("no_key", None, "no key")) as lookup:
            out = self._run(apply=True)
        assert lookup.call_count == 1
        assert em.RequestMapPoint.objects.count() == 0
        assert "stopped: no Google key" in out

    def test_cron_endpoint_needs_the_secret_and_is_dry_unless_execute(self):
        from django.test import Client, override_settings

        from digest.cron_views import GeocodeAccountMapView, _registered_views

        assert _registered_views()["geocode-account-map"] is GeocodeAccountMapView
        secret = "test-cron-secret-value-only-for-tests"
        url = "/internal/cron/geocode-account-map"
        req = self._request(TEMPE)
        http = Client()
        with override_settings(INTERNAL_CRON_SECRET=secret):
            assert http.post(url).status_code in (401, 403)
            with mock.patch(STATUS, return_value="geocoding: test"), mock.patch(
                LOOKUP, return_value=("ok", (33.334, -111.912), "")
            ) as lookup:
                dry = http.post(f"{url}?tenant={self.tenant.id}", HTTP_X_CRON_SECRET=secret)
                assert dry.status_code == 200
                assert dry.json()["executed"] is False
                lookup.assert_not_called()
                assert not em.RequestMapPoint.objects.exists()

                real = http.post(
                    f"{url}?execute=true&tenant={self.tenant.id}&limit=5",
                    HTTP_X_CRON_SECRET=secret,
                )
                assert real.status_code == 200
                assert real.json()["executed"] is True
                assert em.RequestMapPoint.objects.get(request=req).status == "ok"
