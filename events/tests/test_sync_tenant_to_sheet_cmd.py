"""Invocation smoke-test for sync_tenant_to_sheet.

Compile checks can't catch a broken function boundary in a management
command (the merged body is still valid syntax) — actually invoking the
command through call_command exercises add_arguments + the head of
handle, which is exactly where that class of breakage bites.
"""
import pytest
from django.core.management import CommandError, call_command


@pytest.mark.django_db
def test_command_parses_args_and_rejects_unknown_tenant():
    with pytest.raises(CommandError, match="No tenants matched"):
        call_command(
            "sync_tenant_to_sheet",
            "--tenant-slug", "definitely-not-a-tenant",
            "--since-date", "2026-07-01",
            "--delete-rows", "4,5",
        )
