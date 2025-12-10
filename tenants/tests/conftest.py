"""
Pytest configuration for tenants app tests.

This file configures the test database to use PostgreSQL (spark_tests database)
to avoid affecting the development PostgreSQL database.
"""

import pytest
from django.conf import settings
import environ

env = environ.Env()


@pytest.fixture(scope='session')
def django_db_modify_db_settings():
    """
    Override database settings to use PostgreSQL spark_tests database for tests.
    This ensures tests don't affect the development PostgreSQL database.

    This fixture runs before the database is created and modifies the
    database settings to use PostgreSQL with spark_tests database.
    """
    # Start with the default database settings to preserve all required keys
    # (like AUTOCOMMIT, ATOMIC_REQUESTS, etc.)
    db_config = settings.DATABASES['default'].copy()

    # Override only the connection parameters for the test database
    # DATABASE_URL format: postgres:///spark_tests
    # This means: localhost, default port (5432), default user, database=spark_tests
    import os
    os.environ['DATABASE_URL'] = os.environ.get('TEST_DATABASE_URL')
    test_db_config = env.db()
    db_config.update({
        'ENGINE': test_db_config.get('ENGINE', db_config.get('ENGINE')),
        'NAME': test_db_config.get('NAME', 'spark_tests'),
        'USER': test_db_config.get('USER', db_config.get('USER', '')),
        'PASSWORD': test_db_config.get('PASSWORD', db_config.get('PASSWORD', '')),
        'HOST': test_db_config.get('HOST', db_config.get('HOST', '')),
        'PORT': test_db_config.get('PORT', db_config.get('PORT', '')),
    })

    # Add test-specific connection settings
    db_config['CONN_MAX_AGE'] = 0  # Disable persistent connections for tests
    # Disable connection health checks for tests
    db_config['CONN_HEALTH_CHECKS'] = False
    # Ensure OPTIONS dict exists (required by PostgreSQL backend)
    if 'OPTIONS' not in db_config:
        db_config['OPTIONS'] = {}

    settings.DATABASES['default'] = db_config


@pytest.fixture(autouse=True)
def enable_db_access_for_all_tests(db):
    """
    Enable database access for all tests.
    This fixture is automatically applied to all tests.
    """
    pass
