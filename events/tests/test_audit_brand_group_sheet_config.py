"""audit_brand_group_sheet_config: read-only standing code / sheet audit."""

from io import StringIO

import pytest
from django.core.management import call_command

from events.brand_group_sheet import FAMUS_CHECKIN_CODE
from tenants.models import Tenant


@pytest.mark.django_db
def test_audit_flags_checkin_mismatch():
    from django.contrib.auth import get_user_model

    from tenants.tests.base import ensure_role

    role = ensure_role("System")
    user = get_user_model().objects.create_user(
        username="audit-bg-user",
        email="audit-bg@igniteproductions.co",
        role=role,
    )
    tenant = Tenant.objects.create(
        name="Famus Energy",
        slug="famus-energy",
        checkin_code="WRONG-CODE",
        created_by=user,
    )
    out = StringIO()
    call_command("audit_brand_group_sheet_config", stdout=out)
    text = out.getvalue()
    assert "MISMATCH checkin_code" in text
    assert FAMUS_CHECKIN_CODE in text

    tenant.checkin_code = FAMUS_CHECKIN_CODE
    tenant.save(update_fields=["checkin_code"])
    out2 = StringIO()
    call_command("audit_brand_group_sheet_config", stdout=out2)
    assert "MISMATCH checkin_code" not in out2.getvalue()
    assert "OK checkin_code" in out2.getvalue()
