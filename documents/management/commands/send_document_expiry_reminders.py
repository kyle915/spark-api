"""
Daily document-expiry reminder push for ambassadors.

Finds every active AmbassadorDocument with an `expires_on` falling within
the next N days (default 14), and pushes the owning BA a reminder to renew
+ re-upload. Also flips docs already past expiry to status=expired.

Mirrors jobs/management/commands/send_new_gig_digest.py. Best-effort, not
transactional — the GHA schedule fires it once a day. Running twice in a
day double-notifies; acceptable for a nudge.

Usage:
    python manage.py send_document_expiry_reminders                # 14d window
    python manage.py send_document_expiry_reminders --days 30
    python manage.py send_document_expiry_reminders --dry-run
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Push each BA a reminder for documents expiring within N days. "
        "Best-effort; run once daily."
    )

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14,
                            help="Look-ahead window in days (default 14).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Log who would be notified, send nothing.")

    def handle(self, *args, **opts):
        from ambassadors.models import PushDevice
        from ambassadors.push import _send_push_to_user_sync
        from documents import models as dm

        days = max(1, int(opts["days"]))
        dry = bool(opts["dry_run"])
        today = timezone.now().date()
        horizon = today + timedelta(days=days)

        # 1) Mark already-expired active docs as expired (housekeeping).
        newly_expired = dm.AmbassadorDocument.objects.filter(
            status=dm.DocumentStatus.ACTIVE,
            expires_on__isnull=False,
            expires_on__lt=today,
        )
        expired_count = 0
        if not dry:
            expired_count = newly_expired.update(status=dm.DocumentStatus.EXPIRED)
        else:
            expired_count = newly_expired.count()

        # 2) Find docs expiring within the window (today..horizon inclusive).
        expiring = list(
            dm.AmbassadorDocument.objects
            .select_related("ambassador", "ambassador__user")
            .filter(
                status=dm.DocumentStatus.ACTIVE,
                expires_on__isnull=False,
                expires_on__gte=today,
                expires_on__lte=horizon,
            )
        )
        if not expiring:
            self.stdout.write(
                f"No documents expiring in the next {days}d. "
                f"(marked {expired_count} expired)"
            )
            return

        # Only BAs with an active push device are reachable.
        device_user_ids = set(
            PushDevice.objects.filter(is_active=True).values_list("user_id", flat=True)
        )

        # Group expiring docs by BA user.
        by_user: dict[int, list] = {}
        for doc in expiring:
            uid = doc.ambassador.user_id
            if uid not in device_user_ids:
                continue
            by_user.setdefault(uid, []).append(doc)

        sent = 0
        for uid, docs in by_user.items():
            title, body = self._compose(docs, today)
            if dry:
                self.stdout.write(f"[dry-run] user={uid} docs={len(docs)} :: {body}")
                sent += 1
                continue
            try:
                _send_push_to_user_sync(
                    uid,
                    title=title,
                    body=body,
                    # data.screen routes the tap. "profile" lands the BA on
                    # the Profile tab, where the "My documents" entry lives.
                    data={"screen": "profile", "type": "document_expiry",
                          "count": len(docs)},
                )
                sent += 1
            except Exception:
                logger.exception("document expiry push failed user=%s", uid)

        self.stdout.write(
            f"document expiry: notified {sent} BA(s) about docs expiring "
            f"within {days}d; marked {expired_count} expired."
        )

    @staticmethod
    def _compose(docs: list, today) -> tuple[str, str]:
        labels = {
            "government_id": "Government ID",
            "food_handler": "Food Handler Card",
            "alcohol_cert": "Alcohol Server Cert",
            "drivers_license": "Driver's License",
            "certification": "Certification",
            "other": "Document",
        }
        if len(docs) == 1:
            d = docs[0]
            name = d.title or labels.get(d.doc_type, "Document")
            try:
                dleft = (d.expires_on - today).days
            except Exception:
                dleft = None
            when = (
                "today" if dleft == 0
                else f"in {dleft} day{'s' if dleft != 1 else ''}" if dleft is not None
                else "soon"
            )
            return (
                "Document expiring soon",
                f"Your {name} expires {when}. Tap to renew and re-upload.",
            )
        return (
            "Documents expiring soon",
            f"{len(docs)} of your documents expire soon. Tap to review your vault.",
        )
