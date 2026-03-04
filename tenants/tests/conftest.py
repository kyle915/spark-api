"""
Pytest configuration for tenants app tests.

This file configures the test database to use PostgreSQL (spark_tests database)
via TEST_DATABASE_URL so tests never touch the development database.

If you see "relation \"tenants_*\" does not exist" errors, recreate the test DB
once with: uv run python -m pytest tenants --no-reuse-db
"""

import os

import pytest
import environ
from django.conf import settings

env = environ.Env()

# Default test DB URL when TEST_DATABASE_URL is not set (e.g. CI or env without .env).
TEST_DATABASE_URL_DEFAULT = "postgres:///spark_tests"


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    """
    Override database settings to use the test database from TEST_DATABASE_URL.

    Uses TEST_DATABASE_URL from the environment, or postgres:///spark_tests if unset,
    so the test run never uses the development database.
    """
    # Preserve required keys (AUTOCOMMIT, ATOMIC_REQUESTS, etc.)
    db_config = settings.DATABASES["default"].copy()

    test_url = os.environ.get("TEST_DATABASE_URL") or TEST_DATABASE_URL_DEFAULT
    os.environ["DATABASE_URL"] = test_url
    test_db_config = env.db()
    # Ensure test DB is always the one from TEST_DATABASE_URL (default: postgres:///spark_tests)
    db_config.update(
        {
            "ENGINE": test_db_config.get("ENGINE", db_config.get("ENGINE")),
            "NAME": test_db_config.get("NAME", "spark_tests"),
            "USER": test_db_config.get("USER", db_config.get("USER", "")),
            "PASSWORD": test_db_config.get("PASSWORD", db_config.get("PASSWORD", "")),
            "HOST": test_db_config.get("HOST", db_config.get("HOST", "")),
            "PORT": test_db_config.get("PORT", db_config.get("PORT", "")),
        }
    )

    # Add test-specific connection settings
    db_config["CONN_MAX_AGE"] = 0  # Disable persistent connections for tests
    # Disable connection health checks for tests
    db_config["CONN_HEALTH_CHECKS"] = False
    # Ensure OPTIONS dict exists (required by PostgreSQL backend)
    if "OPTIONS" not in db_config:
        db_config["OPTIONS"] = {}

    # Use test database name exactly "spark_tests" (no test_ prefix)
    db_config.setdefault("TEST", {})["NAME"] = "spark_tests"

    settings.DATABASES["default"] = db_config


@pytest.fixture(autouse=True)
def enable_db_access_for_all_tests(db):
    """
    Enable database access for all tests.
    This fixture is automatically applied to all tests.
    """
    pass


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations_for_reused_db(django_db_modify_db_settings, django_db_setup, django_db_blocker):
    """
    When using --reuse-db, ensure the test DB schema is up-to-date by running
    migrations once per session. No-op when the DB is created fresh (no --reuse-db).
    """
    from django.core.management import call_command

    with django_db_blocker.unblock():
        call_command("migrate", interactive=False, verbosity=0)
