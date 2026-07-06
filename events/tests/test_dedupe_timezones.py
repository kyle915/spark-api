"""Tests for the duplicate-TimeZone dedupe fix (fix/dedupe-timezones).

Covers:
  * the data-migration dedupe function collapses duplicate TimeZone rows,
    repoints Request.timezone + Event.timezone to the lowest-id survivor,
    and loses no data;
  * the unique constraint on (name, code, offset) rejects new duplicates;
  * the TimeZoneQueriesService queryset is distinct (defense-in-depth).

Run on Postgres (postgres:///spark_tests).
"""

import importlib

import pytest
from django.apps import apps as global_apps
from django.db import IntegrityError, transaction

from tenants.models import Role, Tenant

from events.models import Event, Request, RequestType, TimeZone
from events.queries import TimeZoneQueriesService

# Import the migration module so we can call its dedupe function directly.
dedupe_migration = importlib.import_module("events.migrations.0048_dedupe_timezones")


@pytest.fixture
def user(django_user_model):
    from tenants.tests.base import ensure_role
    from utils.utils import ROLE_ID
    role = ensure_role("Client", slug="client", pk=ROLE_ID.Client)
    return django_user_model.objects.create_user(
        username="dedupe_user",
        email="dedupe@test.com",
        password="testpass123",
        role=role,
    )


@pytest.fixture
def tenant(user):
    return Tenant.objects.create(name="Dedupe Tenant", created_by=user)


@pytest.fixture
def request_type(tenant, user):
    return RequestType.objects.create(
        name="Dedupe Request Type", tenant=tenant, created_by=user
    )


def _drop_unique_constraint():
    """Drop the unique constraint so we can seed duplicate rows in-test.

    The constraint exists on the migrated test DB; to reproduce the pre-fix
    state (duplicate rows) we temporarily drop it, seed dupes, run the dedupe,
    then re-add it.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE events_timezone "
            "DROP CONSTRAINT IF EXISTS uq_timezone_name_code_offset"
        )


def _add_unique_constraint():
    from django.db import connection

    with connection.cursor() as cursor:
        # "offset" is a reserved word in Postgres, so it must be quoted.
        cursor.execute(
            "ALTER TABLE events_timezone "
            "ADD CONSTRAINT uq_timezone_name_code_offset "
            'UNIQUE (name, code, "offset")'
        )


@pytest.mark.django_db(transaction=True)
class TestDedupeTimezones:
    def test_dedupe_collapses_dupes_and_repoints_fks(
        self, tenant, request_type, user
    ):
        """Duplicate zones collapse to the lowest-id survivor; FKs repoint."""
        _drop_unique_constraint()
        try:
            # Seed 3 semantically-identical "EST" zones (same name/code/offset)
            # plus a distinct "PST" zone that must be left untouched.
            est_a = TimeZone.objects.create(name="Eastern", code="EST", offset=-300)
            est_b = TimeZone.objects.create(name="Eastern", code="EST", offset=-300)
            est_c = TimeZone.objects.create(name="Eastern", code="EST", offset=-300)
            pst = TimeZone.objects.create(name="Pacific", code="PST", offset=-480)

            survivor_id = min(est_a.id, est_b.id, est_c.id)
            duplicate_ids = {est_a.id, est_b.id, est_c.id} - {survivor_id}

            # A Request points at a DUPLICATE (not the survivor).
            req = Request.objects.create(
                name="Dedupe Request",
                address="123 Test St",
                request_type=request_type,
                tenant=tenant,
                created_by=user,
                timezone=est_b,
            )
            # An Event points at a different DUPLICATE.
            evt = Event.objects.create(
                name="Dedupe Event",
                address="123 Test St",
                tenant=tenant,
                created_by=user,
                timezone=est_c,
            )

            assert TimeZone.objects.filter(code="EST").count() == 3

            # Run the dedupe (same callable the data migration runs).
            dedupe_migration.dedupe_timezones(global_apps, None)

            # Dupes collapsed: only the survivor EST + the untouched PST remain.
            remaining_est = list(TimeZone.objects.filter(code="EST"))
            assert len(remaining_est) == 1
            assert remaining_est[0].id == survivor_id
            assert not TimeZone.objects.filter(id__in=duplicate_ids).exists()

            # Distinct PST zone is preserved (no data lost).
            assert TimeZone.objects.filter(id=pst.id).exists()
            assert TimeZone.objects.count() == 2

            # FKs repointed to the survivor (same semantic zone — lossless).
            req.refresh_from_db()
            evt.refresh_from_db()
            assert req.timezone_id == survivor_id
            assert evt.timezone_id == survivor_id
        finally:
            _add_unique_constraint()

    def test_dedupe_is_idempotent_with_no_dupes(self, tenant):
        """Running dedupe on already-clean data is a harmless no-op."""
        _drop_unique_constraint()
        try:
            tz = TimeZone.objects.create(name="Central", code="CST", offset=-360)
            before = TimeZone.objects.count()

            dedupe_migration.dedupe_timezones(global_apps, None)

            assert TimeZone.objects.count() == before
            assert TimeZone.objects.filter(id=tz.id).exists()
        finally:
            _add_unique_constraint()

    def test_unique_constraint_rejects_new_duplicate(self):
        """The (name, code, offset) unique constraint blocks new dupes."""
        TimeZone.objects.create(name="Mountain", code="MST", offset=-420)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                TimeZone.objects.create(name="Mountain", code="MST", offset=-420)

    def test_unique_constraint_allows_distinct_zone(self):
        """A zone differing in any key field is still allowed."""
        TimeZone.objects.create(name="Alaska", code="AKST", offset=-540)
        # Different offset -> different semantic zone -> allowed.
        TimeZone.objects.create(name="Alaska", code="AKST", offset=-480)
        assert TimeZone.objects.filter(code="AKST").count() == 2

    def test_resolver_queryset_is_distinct(self):
        """TimeZoneQueriesService applies .distinct() to its queryset."""
        TimeZone.objects.create(name="Hawaii", code="HST", offset=-600)
        service = TimeZoneQueriesService()
        qs = service.get_filtered_queryset()
        assert qs.query.distinct is True
