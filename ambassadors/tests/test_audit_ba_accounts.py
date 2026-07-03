"""Invocation smoke for audit_ba_accounts — the command must run end to end
via call_command (a compile check can't catch a broken handle boundary)."""

import io

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db


def test_audit_runs_and_reports_sections():
    out = io.StringIO()
    call_command(
        "audit_ba_accounts",
        "--names", "Nobody Nowhere",
        "--tenant-slug", "no-such-tenant",
        "--deactivate-empty-relay-dups",
        stdout=out,
    )
    text = out.getvalue()
    assert "RELAY ACCOUNTS" in text
    assert "TENANT-LESS AMBASSADOR CENSUS" in text
    assert "BACKEND ERRORS" in text
    assert "no tenant with slug" in text
    # dry-run by default — nothing to write against an empty DB
    assert "section failed" not in text
