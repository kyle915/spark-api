"""Read-only access-misuse review (``audit_access_misuse`` /
``/internal/cron/audit-access-misuse``).

Seeds one example of each thing the holes in #1424/#1426/#1427 allowed and
checks that the report surfaces it, that ordinary activity stays out, that
only elevated accounts appear with an email, and that nothing is written.
"""
from __future__ import annotations

import io
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from ambassadors.models import Ambassador, Attendance
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import PayrollApproval, Request, RequestActivityLog, RequestType
from tenants.misuse_audit import build_misuse_audit
from tenants.models import TenantedUser, User

SECRET = "test-cron-secret-value-only-for-tests"


@pytest.mark.django_db(transaction=True)
class TestAccessMisuseAudit(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.a = self.create_tenant(name="Brand A")
        self.b = self.create_tenant(name="Brand B")

        self.admin = self.create_user(
            username="ops@igniteproductions.co",
            email="ops@igniteproductions.co",
            role=self.roles["spark_admin"],
        )
        self.client_a = self.create_user(
            username="buyer@brand-a.test", email="buyer@brand-a.test", role=self.roles["client"]
        )
        self.create_tenanted_user(self.client_a, self.a)
        ba_user = self.create_user(
            username="ba.person@ba.test", email="ba.person@ba.test", role=self.roles["ambassador"]
        )
        self.create_tenanted_user(ba_user, self.a)
        self.ba = self.create_ambassador(ba_user)
        self.ba_user = ba_user

        # 1. Self-made admin with an outside email (unauthenticated inviteUser).
        self.rogue = self.create_user(
            username="rogue@gmail.test", email="rogue@gmail.test", role=self.roles["spark_admin"]
        )
        # 2. A client disables a BA (disableAmbassador).
        Ambassador.objects.filter(id=self.ba.id).update(
            is_active=False, updated_by=self.client_a, updated_at=timezone.now()
        )
        # 3. The BA lands in a second brand.
        self.create_tenanted_user(ba_user, self.b)
        # 4. A request re-homed to brand B after a log row was written in A,
        #    with its event left behind in A and last edited by the client.
        rtype = RequestType.objects.create(name="Sampling", tenant=self.a, created_by=self.admin)
        self.req = Request.objects.create(
            name="Moved", address="1 Main", request_type=rtype, tenant=self.a,
            created_by=self.admin, date=timezone.now(),
        )
        RequestActivityLog.objects.create(
            tenant=self.a, request=self.req, kind=RequestActivityLog.KIND_CREATED
        )
        etype = self.create_event_type("Sampling", self.a)
        self.event = self.create_event(
            name="Moved event", tenant=self.a, address="1 Main", event_type=etype,
            date=timezone.now(), request=self.req,
        )
        Request.objects.filter(id=self.req.id).update(tenant=self.b, updated_by=self.client_a)
        # 5. A client writes a punch for the BA; the BA writes their own.
        self.bad_punch = Attendance.objects.create(
            clock_time=timezone.now(), ambassador=self.ba, event=self.event, created_by=self.client_a
        )
        self.own_punch = Attendance.objects.create(
            clock_time=timezone.now(), ambassador=self.ba, event=self.event, created_by=ba_user
        )
        # 6. A client approves and marks paid.
        today = timezone.now().date()
        self.approval = PayrollApproval.objects.create(
            tenant=self.a, ambassador=self.ba, period_start=today, period_end=today,
            hours=Decimal("4"), approved_by=self.client_a, paid_at=timezone.now(),
        )

    def _audit(self):
        now = timezone.now()
        return build_misuse_audit(start=now - timedelta(days=1), end=now + timedelta(hours=1))

    def test_each_seeded_misuse_is_reported(self):
        data = self._audit()
        s = data["sections"]

        rogue = next(r for r in s["users_created"]["rows"] if r["user_id"] == self.rogue.id)
        assert "elevated_non_ignite_email" in rogue["flags"]
        assert rogue["email"] == "rogue@gmail.test"
        assert any(
            r["email"] == "rogue@gmail.test" and not r["ignite_email"]
            for r in s["elevated_accounts"]["rows"]
        )

        bas = s["deactivations"]["ambassadors"]
        assert bas["flag_non_admin_not_self"] == 1
        assert bas["rows"][0]["ambassador_id"] == self.ba.id

        joined = s["memberships"]["ambassador_joined_another_tenant"]["rows"]
        assert [(r["user_id"], r["tenant_id"]) for r in joined] == [(self.ba_user.id, self.b.id)]

        tm = s["tenant_moves"]
        assert tm["history_table"] is None
        assert tm["activity_log_tenant_mismatch"]["rows"][0]["request_id"] == self.req.id
        assert tm["event_vs_request_tenant_mismatch"]["rows"][0]["event_id"] == self.event.id
        moved = tm["non_admin_edits_outside_own_tenants"]["by_model"]["events.Request"]["rows"]
        assert moved[0]["id"] == self.req.id
        assert "editor_not_tied_to_row_tenant" in moved[0]["reasons"]

        punches = s["attendance"]["rows"]
        assert [p["attendance_id"] for p in punches] == [self.bad_punch.id]

        assert [r["approval_id"] for r in s["payroll"]["approved_by_non_admin"]["rows"]] == [
            self.approval.id
        ]
        assert s["payroll"]["marked_paid"]["count"] == 1
        assert s["uploads"]["derivable"] is False
        assert s["password_changes"]["recorded"] is False

    def test_only_elevated_accounts_carry_an_email(self):
        blob = json.dumps(self._audit(), default=str)
        assert "buyer@brand-a.test" not in blob
        assert "ba.person@ba.test" not in blob
        assert "rogue@gmail.test" in blob
        assert "ops@igniteproductions.co" in blob

    def test_default_window_stops_at_the_fix(self):
        # Rows created now are after the fix merge times, so the default
        # window (since 2026-06-01, up to each fix) leaves them out.
        data = build_misuse_audit()
        ids = {r["user_id"] for r in data["sections"]["users_created"]["rows"]}
        assert self.rogue.id not in ids
        assert data["window"]["end_pr_1424"].startswith("2026-11-16T11:20")

    def test_command_is_read_only(self):
        before = (
            User.objects.count(), TenantedUser.objects.count(), Attendance.objects.count(),
            PayrollApproval.objects.count(), Request.objects.count(),
        )
        out = io.StringIO()
        now = timezone.now()
        call_command(
            "audit_access_misuse", start=(now - timedelta(days=1)).isoformat(),
            end=(now + timedelta(hours=1)).isoformat(), as_json=True, stdout=out,
        )
        raw = out.getvalue()
        data = json.loads(raw.split("MISUSEAUDIT_JSON_START", 1)[1].split("MISUSEAUDIT_JSON_END", 1)[0])
        assert data["summary"]["attendance_written_by_other_non_admin"] == 1
        after = (
            User.objects.count(), TenantedUser.objects.count(), Attendance.objects.count(),
            PayrollApproval.objects.count(), Request.objects.count(),
        )
        assert before == after
        assert Request.objects.get(id=self.req.id).tenant_id == self.b.id

    @override_settings(INTERNAL_CRON_SECRET=SECRET)
    def test_cron_endpoint(self):
        client = Client()
        assert client.post("/internal/cron/audit-access-misuse").status_code == 401
        now = timezone.now()
        resp = client.post(
            "/internal/cron/audit-access-misuse",
            {"start": (now - timedelta(days=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()},
            HTTP_X_CRON_SECRET=SECRET,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["summary"]["payroll_approved_by_non_admin"] == 1
        bad = client.post("/internal/cron/audit-access-misuse", {"start": "nope"}, HTTP_X_CRON_SECRET=SECRET)
        assert bad.status_code == 400
