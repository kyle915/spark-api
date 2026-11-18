"""List and revoke Spark MCP (AI app) connections.

    manage.py mcp_connections                       # active connections
    manage.py mcp_connections --all                 # include revoked
    manage.py mcp_connections --revoke <uuid>
    manage.py mcp_connections --revoke-user someone@brand.com
    manage.py mcp_connections --revoke-tenant 17
    manage.py mcp_connections --disable-client <client_id>
    manage.py mcp_connections --usage 17            # last 7 days of calls for tenant 17
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone

from spark_mcp.models import McpClient, McpConnection, McpToolCall


class Command(BaseCommand):
    help = "List and revoke Spark MCP (AI app) connections."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--revoke")
        parser.add_argument("--revoke-user")
        parser.add_argument("--revoke-tenant", type=int)
        parser.add_argument("--disable-client")
        parser.add_argument("--usage", type=int, metavar="TENANT_ID")

    def handle(self, *args, **opts):
        reason = "revoked by staff (mcp_connections)"
        if opts["revoke"]:
            conn = McpConnection.objects.filter(uuid=opts["revoke"]).first()
            if conn is None:
                raise CommandError("No such connection.")
            conn.revoke(reason=reason)
            self.stdout.write(f"Revoked {conn}")
            return
        if opts["revoke_user"] or opts["revoke_tenant"]:
            qs = McpConnection.objects.filter(revoked_at__isnull=True)
            if opts["revoke_user"]:
                qs = qs.filter(user__email__iexact=opts["revoke_user"])
            if opts["revoke_tenant"]:
                qs = qs.filter(tenant_id=opts["revoke_tenant"])
            n = 0
            for conn in qs:
                conn.revoke(reason=reason)
                n += 1
            self.stdout.write(f"Revoked {n} connection(s).")
            return
        if opts["disable_client"]:
            n = McpClient.objects.filter(client_id=opts["disable_client"]).update(disabled=True)
            self.stdout.write(f"Disabled {n} client(s); their tokens stop working at once.")
            return
        if opts["usage"]:
            since = timezone.now() - timedelta(days=7)
            rows = (
                McpToolCall.objects.filter(tenant_id=opts["usage"], created_at__gte=since)
                .values("tool", "status")
                .annotate(n=Count("id"))
                .order_by("tool", "status")
            )
            for r in rows:
                self.stdout.write(f"{r['tool']:28} {r['status']:13} {r['n']}")
            return
        qs = McpConnection.objects.select_related("user", "tenant", "client").order_by("-created_at")
        if not opts["all"]:
            qs = qs.filter(revoked_at__isnull=True)
        for c in qs[:500]:
            state = "revoked" if c.revoked_at else "active"
            self.stdout.write(
                f"{c.uuid}  {state:7}  {c.tenant.name:24.24}  {c.user.email:32.32}  "
                f"{c.client.display_name:20.20}  last used {c.last_used_at or '-'}"
            )
