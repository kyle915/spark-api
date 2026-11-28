"""
Root pytest conftest: set test database URL before Django loads.

Must run before any import that loads config.settings, so that env.db()
in settings uses TEST_DATABASE_URL (postgres:///spark_tests) for the test run.
"""
import os

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "postgres:///spark_tests"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _legal_gate_off_by_default(settings):
    """The legal-acceptance gate (legal/graphql_extension.py) would block
    every existing schema test's admin/client user. Off for the suite; the
    legal tests switch it back on with the `settings` fixture. (An env var
    set here is too late: pytest-django has already loaded settings.)

    LEGAL_ACCEPTANCE_ENFORCED=true in the shell keeps it ON for the whole run,
    to check which suites the gate touches (BA check-in, MCP, cron: none)."""
    if os.environ.get("LEGAL_ACCEPTANCE_ENFORCED", "").lower() != "true":
        settings.LEGAL_ACCEPTANCE_ENFORCED = False
    if "ABUSE_USAGE_FLUSH_INTERVAL_SECONDS" not in os.environ:
        settings.ABUSE_USAGE_FLUSH_INTERVAL_SECONDS = 0.0
    yield
