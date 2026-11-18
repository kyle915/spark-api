"""Read-only: did anyone abuse the Ignite-domain admin grant or the open
register mutations? See :mod:`tenants.ignite_domain_audit`.

    python manage.py audit_ignite_domain_accounts
    python manage.py audit_ignite_domain_accounts --json

READ-ONLY. Emails only for Ignite-domain accounts; everyone else is an id.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from tenants.ignite_domain_audit import build_ignite_domain_audit

JSON_START = "IGNITEDOMAINAUDIT_JSON_START"
JSON_END = "IGNITEDOMAINAUDIT_JSON_END"


class Command(BaseCommand):
    help = "Read-only audit of Ignite-domain accounts, elevated accounts, and MCP connections."

    def add_arguments(self, parser):
        parser.add_argument("--cap", type=int, default=500, help="max rows per list")
        parser.add_argument(
            "--json", action="store_true", dest="as_json",
            help=f"print the payload between {JSON_START}/{JSON_END} markers",
        )

    def handle(self, *args, **opts):
        cap = max(1, min(int(opts.get("cap") or 500), 5000))
        payload = build_ignite_domain_audit(cap=cap)
        payload["generated_at"] = timezone.now().isoformat()

        w = self.stdout.write
        w("Ignite-domain / elevated-account audit (read-only)")
        for key, value in payload["summary"].items():
            w(f"  {key:<42} {value}")

        w("\nSUSPICIOUS Ignite-domain accounts (ambassador / unverified / inactive / BA profile / case-dup):")
        for r in payload["ignite_domain_suspicious"]:
            w(
                f"  #{r['id']} {r['email']} role={r['role']} verified={r['verified']} "
                f"active={r['is_active']} created={r['created_at']} last_login={r['last_login']} "
                f"refresh_tokens={r['refresh_tokens']} admin_before={r['admin_before_fix']} "
                f"reasons={','.join(r['reasons'])} "
                f"memberships={[m['tenant_id'] for m in r['memberships']]}"
            )
        w("\nStaff-looking accounts that LOSE admin under the new rule (verify_user them before shipping):")
        for r in payload["staff_losing_admin_after_fix"]:
            w(f"  #{r['id']} {r['email']} role={r['role']} verified={r['verified']} last_login={r['last_login']}")
        w("\nAll Ignite-domain accounts:")
        for r in payload["ignite_domain_accounts"]:
            w(
                f"  #{r['id']} {r['email']} role={r['role']} verified={r['verified']} "
                f"active={r['is_active']} staff={r['is_staff']} super={r['is_superuser']} "
                f"admin before/after={r['admin_before_fix']}/{r['admin_after_fix']} "
                f"created={r['created_at']} last_login={r['last_login']}"
            )
        w("\nspark-admin / is_staff accounts on OTHER domains (email domain only):")
        for r in payload["elevated_accounts_other_domains"]:
            w(
                f"  #{r['id']} @{r['email_domain']} role={r['role']} staff={r['is_staff']} "
                f"super={r['is_superuser']} verified={r['verified']} active={r['is_active']} "
                f"created={r['created_at']} last_login={r['last_login']}"
            )
        mcp = payload["mcp"]
        w(f"\nMCP connections: {mcp.get('count')}  tool calls: {mcp.get('tool_calls')}")
        for c in mcp.get("connections", []):
            w(
                f"  conn#{c['id']} user#{c['user_id']} {c['user_email'] or ''} role={c['user_role']} "
                f"tenant#{c['tenant_id']} {c['tenant']!r} client={c['client_name']!r} "
                f"created={c['created_at']} last_used={c['last_used_at']} revoked={c['revoked_at']} "
                f"calls={c['tool_calls']} last_call={c['last_tool_call_at']}"
            )

        if opts.get("as_json"):
            w(JSON_START)
            w(json.dumps(payload, default=str))
            w(JSON_END)
