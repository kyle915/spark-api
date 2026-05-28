"""
Daily "you've been paid" push for ambassadors.

Wingspan holds the payments (we don't persist them), so this polls the
Wingspan API for recently-sent disbursements, maps each to a BA by
contractor email, and pushes a "payment sent" notification — once per
payment, deduped via NotifiedWingspanPayment so the daily cadence never
double-notifies.

First-run / catch-up safety: payments with a parseable pay_date older
than --since-days are ledgered but NOT pushed, so enabling this doesn't
blast everyone about historical payments. Undated payments still push
(dedup keeps it to once).

Intended to run once a day from a cron runner (GitHub Actions hits
`/internal/cron/send-payment-notifications`).

Usage:
    python manage.py notify_payments_sent
    python manage.py notify_payments_sent --since-days 14
    python manage.py notify_payments_sent --dry-run
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)

_SENT_STATUSES = {
    "sent", "paid", "complete", "completed", "succeeded", "success",
    "disbursed", "deposited",
}
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


class Command(BaseCommand):
    help = (
        "Push a 'you've been paid' notification for each newly-sent "
        "Wingspan payment, mapped to the BA by contractor email. "
        "Deduped; run once daily."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Max payments to pull from Wingspan (default 100).",
        )
        parser.add_argument(
            "--since-days",
            type=int,
            default=10,
            help="Don't push for payments dated older than this (still "
                 "ledgered). Undated payments always push. Default 10.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log what would be pushed/ledgered, but change nothing.",
        )

    def handle(self, *args, **opts):
        from ambassadors.push import _send_push_to_user_sync
        from wingspan import client
        from wingspan.models import NotifiedWingspanPayment

        if not client.is_connected():
            self.stdout.write("Wingspan not configured; nothing to do.")
            return

        limit = max(1, int(opts["limit"]))
        since_days = max(0, int(opts["since_days"]))
        dry = bool(opts["dry_run"])
        cutoff = (timezone.now() - timedelta(days=since_days)).date()

        try:
            payments = asyncio.run(client.list_payments(limit=limit))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Wingspan list_payments failed")
            self.stderr.write(f"Wingspan fetch failed: {exc}")
            return

        sent = [
            p for p in payments
            if p.id and (p.status or "").strip().lower() in _SENT_STATUSES
        ]
        if not sent:
            self.stdout.write("No sent payments returned by Wingspan.")
            return

        already = set(
            NotifiedWingspanPayment.objects.filter(
                payment_id__in=[p.id for p in sent]
            ).values_list("payment_id", flat=True)
        )
        User = get_user_model()

        pushed = skipped_known = unmapped = suppressed_old = 0
        for p in sent:
            if p.id in already:
                skipped_known += 1
                continue

            email = (p.contractor_email or "").strip().lower()
            user = (
                User.objects.filter(email__iexact=email).first()
                if email else None
            )
            recent = self._is_recent(p.pay_date, cutoff)

            if dry:
                state = (
                    "no-match" if not user
                    else ("push" if recent else "suppress-old")
                )
                self.stdout.write(
                    f"[dry-run] payment={p.id} email={email or '—'} "
                    f"amount={p.amount} date={p.pay_date or '—'} -> {state}"
                )
                continue

            # Ledger first (even when unmapped/old) so we never reprocess it.
            NotifiedWingspanPayment.objects.create(
                payment_id=p.id,
                user=user,
                amount=self._to_decimal(p.amount),
            )
            if not user:
                unmapped += 1
                continue
            if not recent:
                suppressed_old += 1
                continue

            title = "You've been paid 💸"
            amt = (
                f"${p.amount:,.2f}" if isinstance(p.amount, (int, float))
                else "Your payment"
            )
            body = f"{amt} is on its way from Ignite. Tap to see your earnings."
            try:
                _send_push_to_user_sync(
                    user.id,
                    title=title,
                    body=body,
                    data={"screen": "earnings", "paymentId": p.id},
                )
                pushed += 1
            except Exception:
                logger.exception("payment push failed payment=%s", p.id)

        self.stdout.write(
            f"payment notifications: pushed {pushed}, "
            f"{skipped_known} already-known, {unmapped} unmapped, "
            f"{suppressed_old} suppressed-as-old "
            f"(of {len(sent)} sent payment(s))."
        )

    @staticmethod
    def _is_recent(pay_date, cutoff) -> bool:
        """True unless pay_date parses to a date strictly before cutoff.
        Missing/unparseable dates count as recent (push once anyway)."""
        if not pay_date:
            return True
        m = _DATE_RE.search(str(pay_date))
        if not m:
            return True
        try:
            from datetime import date
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return True
        return d >= cutoff

    @staticmethod
    def _to_decimal(amount):
        if amount is None:
            return None
        try:
            return Decimal(str(amount))
        except (InvalidOperation, ValueError):
            return None
