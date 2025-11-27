"""
Pytest configuration for tenants app tests.

This file configures the test database to use SQLite to avoid affecting
the development PostgreSQL database.
"""
import pytest
from django.conf import settings


@pytest.fixture(scope='session')
def django_db_modify_db_settings():
    """
    Override database settings to use SQLite for tests.
    This ensures tests don't affect the development PostgreSQL database.

    This fixture runs before the database is created and modifies the
    database settings to use SQLite in-memory database.
    """
    # Override database settings to use SQLite in-memory database
    settings.DATABASES['default'] = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }


@pytest.fixture(autouse=True)
def enable_db_access_for_all_tests(db):
    """
    Enable database access for all tests.
    This fixture is automatically applied to all tests.
    """
    pass
