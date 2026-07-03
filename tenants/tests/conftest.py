"""
Pytest configuration for tenants app tests.

The test database comes from TEST_DATABASE_URL via the ROOT conftest, which
maps it onto DATABASE_URL before Django settings load — pytest-django then
derives the real test DB from it (test_<name>). Do NOT override
django_db_modify_db_settings here: a session-scoped fixture defined in a
subdirectory conftest only fires when the first tenants/tests/* test runs,
and re-assigning settings.DATABASES["default"] at that point (after
create_test_db already renamed the active config) poisons every NEW
per-thread connection — async resolvers via sync_to_async — with the
un-renamed base DB name for the rest of the session.

If you see "relation \"tenants_*\" does not exist" errors, recreate the test
DB once with: uv run python -m pytest tenants --no-reuse-db
"""

import pytest


@pytest.fixture(autouse=True)
def enable_db_access_for_all_tests(db):
    """
    Enable database access for all tests.
    This fixture is automatically applied to all tests.
    """
    pass


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations_for_reused_db(django_db_setup, django_db_blocker):
    """
    When using --reuse-db, ensure the test DB schema is up-to-date by running
    migrations once per session. No-op when the DB is created fresh (no --reuse-db).
    """
    from django.core.management import call_command

    with django_db_blocker.unblock():
        call_command("migrate", interactive=False, verbosity=0)
