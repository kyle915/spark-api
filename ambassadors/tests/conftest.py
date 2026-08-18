"""
Pytest configuration for ambassadors app tests.

This file reuses the database configuration from tenants app tests.
"""
# Ensure strawberry_django is imported before any schema imports
import strawberry_django  # noqa: F401
import pytest
from django.core.cache import cache

# Import conftest from tenants to reuse database configuration
# This ensures we use the same PostgreSQL spark_tests database
from tenants.tests.conftest import *


@pytest.fixture(autouse=True)
def _reset_checkin_rate_limits():
    """Public check-in rate limits key on client IP.

    Django's test client is always 127.0.0.1, and LocMemCache lives for
    the whole pytest process. Identify is capped at 10 hits / 5 minutes,
    so extra identify calls in the 3rd-party typed-store tests 429 a
    later standing-link test that expects 400. Reset every ambassador
    test so the suite can't trip itself.
    """
    cache.clear()
