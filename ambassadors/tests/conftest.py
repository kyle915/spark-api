"""
Pytest configuration for ambassadors app tests.

This file reuses the database configuration from tenants app tests.
"""
# Ensure strawberry_django is imported before any schema imports
import strawberry_django  # noqa: F401

# Import conftest from tenants to reuse database configuration
# This ensures we use the same PostgreSQL spark_tests database
from tenants.tests.conftest import *
