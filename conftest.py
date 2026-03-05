"""
Root pytest conftest: set test database URL before Django loads.

Must run before any import that loads config.settings, so that env.db()
in settings uses TEST_DATABASE_URL (postgres:///spark_tests) for the test run.
"""
import os

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "postgres:///spark_tests"
