"""Read-only audit — who has been submitting Requests into a tenant's portal,
and (by submitter role) which submissions triggered the client auto-approve
email that CCs the Ignite team.

Answers "has the client actually submitted anything from their own login
recently, and does it reach us?" without touching prod through the DB directly:
lists a tenant's Requests in a recent window with created_at + the CREATED_BY
user (name / email / role), classifies each as a CLIENT-login submission vs an
Ignite/admin-created one, and summarizes counts.

NEVER writes. Run via the secret-gated cron endpoint
(digest.cron_views.AuditClientSubmissionsView) + the audit-client-submissions
GitHub workflow.

Email-routing note (verified in events/mutations.py): an authenticated CLIENT
submission (`create_request`, role=client) auto-approves and sends the approval
email with To=requestor and CC=_get_request_cc_emails() — the whole Ignite team
(IGNITE_REVIEW_CC + every spark-admin + every active @igniteproductions.co
user). So each client-login submission below with status=approved routed an
email to the Ignite team. (The dedicated `_notify_spark_admins_for_client_request`
hook is a no-op; the Ignite CC rides on the approval email.)
"""

from __future__ import annotations

import datetime
import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as dj_tz

from events.models import Request
from tenants.models import Role, Tenant

IGNITE_DOMAIN = "@igniteproductions.co"


def _submitter_kind(user) -> str:
    """client | ignite | external | system(none)."""
    if user is None:
        return "system/none"
    email = (getattr(user, "email", "") or "").lower()
    role = getattr(user, "role", None)
    slug = (getattr(role, "slug", "") or "").lower()
    if email.endswith(IGNITE_DOMAIN) or slug == Role.SPARK_ADMIN_SLUG or getattr(
        user, "is_staff", False
    ) or getattr(user, "is_superuser", False):
        return "ignite"
    if slug == Role.CLIENT_SLUG:
        return "client"
    return "external"


class Command(BaseCommand):
    help = "Read-only: list a tenant's recent Requests by submitter (client vs Ignite)."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="girl-beer")
        parser.add_argument("--days", type=int, default=45)
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        slug = (opts["tenant_slug"] or "").strip()
        tenant = Tenant.objects.filter(slug__iexact=slug).first()
        if not tenant:
            tenant = Tenant.objects.filter(name__iexact=slug).first()
        if not tenant:
            raise CommandError(f"Tenant not found: {slug!r}")

        days = int(opts["days"])
        cutoff = dj_tz.now() - datetime.timedelta(days=days)
        qs = (
            Request.objects.filter(tenant=tenant, created_at__gte=cutoff)
            .select_related("created_by", "created_by__role", "status", "request_type")
            .order_by("-created_at")[: int(opts["limit"])]
        )

        rows = []
        for r in qs:
            u = r.created_by
            kind = _submitter_kind(u)
            rows.append(
                {
                    "uuid": str(r.uuid),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "submitter_kind": kind,
                    "created_by": (
                        (
                            " ".join(
                                filter(
                                    None,
                                    [
                                        (getattr(u, "first_name", "") or "").strip(),
                                        (getattr(u, "last_name", "") or "").strip(),
                                    ],
                                )
                            ).strip()
                            or getattr(u, "email", None)
                        )
                        if u
                        else None
                    ),
                    "created_by_email": (getattr(u, "email", None) if u else None),
                    "created_by_role": (
                        getattr(getattr(u, "role", None), "slug", None) if u else None
                    ),
                    "requestor_email": (r.requestor_email or None),
                    "status": (getattr(r.status, "slug", None)),
                    "request_type": (getattr(r.request_type, "name", None)),
                    "name": r.name,
                    "deleted": bool(r.deleted_at),
                }
            )

        from collections import Counter

        kinds = Counter(x["submitter_kind"] for x in rows)
        client_rows = [x for x in rows if x["submitter_kind"] == "client"]
        client_approved = sum(
            1 for x in client_rows if (x["status"] or "") == "approved"
        )
        report = {
            "tenant": {"id": tenant.id, "name": tenant.name, "slug": tenant.slug},
            "window_days": days,
            "since": cutoff.isoformat(),
            "total_requests_in_window": len(rows),
            "by_submitter": dict(kinds),
            "client_submissions": len(client_rows),
            "client_submissions_approved_emailed_ignite": client_approved,
            "most_recent": rows[0] if rows else None,
            "rows": rows,
        }

        w = self.stdout.write
        w("")
        w(f"audit_client_submissions — {tenant.name} (last {days}d)")
        w(f"  total requests in window : {len(rows)}")
        w(f"  by submitter            : {dict(kinds)}")
        w(f"  CLIENT-login submissions : {len(client_rows)} "
          f"({client_approved} approved → each CC'd the Ignite team)")
        w("")
        for x in rows[:40]:
            flag = "★CLIENT" if x["submitter_kind"] == "client" else x["submitter_kind"]
            w(f"  {x['created_at']}  [{flag}]  {x['created_by']} "
              f"<{x['created_by_email']}>  · {x['status']} · {x['name']}")
        w("")
        w("JSON_RESULT: " + json.dumps(report, default=str))
