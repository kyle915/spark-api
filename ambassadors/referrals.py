"""BA referral program — code generation, signup attribution, first-shift.

The flow, end to end:

  1. A BA opens "Invite friends" in the app → ``myReferralCode`` lazily
     creates their stable code (:func:`get_or_create_code`) and they share
     it ("join with my code SPK-XXXX").
  2. The friend signs up with the code → the ambassador register mutation
     calls :func:`attribute_signup`, creating an :class:`AmbassadorReferral`
     (referrer → referred, signed_up_at).
  3. The friend clocks out of their FIRST shift → the attendance flow calls
     :func:`complete_first_shift_if_referred`, which stamps
     ``first_shift_completed_at`` and returns the referrer so the caller can
     push "🎉 your friend completed their first shift".

Every entry point is best-effort: a referral failure must never break
signup or clock-out. Payouts stay manual — the table tells Ignite who
earned the bonus; Wingspan moves the money.
"""

from __future__ import annotations

import logging
import secrets

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Unambiguous uppercase alphabet (no 0/O/1/I/L) — codes get read aloud and
# typed on phones. 8 chars over 28 symbols ≈ 4e11 combinations.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_MAX_CODE_ATTEMPTS = 8


def _random_code() -> str:
    return "".join(
        secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH)
    )


def get_or_create_code(user) -> str:
    """The user's stable referral code, creating it on first ask."""
    from ambassadors.models import ReferralCode

    existing = ReferralCode.objects.filter(user=user).first()
    if existing:
        return existing.code
    for _ in range(_MAX_CODE_ATTEMPTS):
        try:
            # Savepoint so an IntegrityError can't poison a caller's
            # surrounding atomic block (tests, future transactional callers).
            with transaction.atomic():
                return ReferralCode.objects.create(
                    user=user, code=_random_code()
                ).code
        except IntegrityError:
            # Either a code collision (retry with a new code) or a
            # concurrent create for the same user (return theirs).
            race = ReferralCode.objects.filter(user=user).first()
            if race:
                return race.code
    raise RuntimeError("Could not allocate a unique referral code.")


def attribute_signup(referral_code: str | None, new_user) -> bool:
    """Record that ``new_user`` signed up with ``referral_code``.

    Returns True when a referral row was created. Silently a no-op (with a
    log line, never an exception) for blank/unknown codes, self-referrals,
    and users that already have a referral — signup must never fail because
    of a bad code.
    """
    from ambassadors.models import AmbassadorReferral, ReferralCode

    code = (referral_code or "").strip().upper()
    if not code or new_user is None:
        return False
    try:
        ref = (
            ReferralCode.objects.filter(code__iexact=code)
            .select_related("user")
            .first()
        )
        if ref is None:
            logger.info("Referral code %r not found — signup unattributed.", code)
            return False
        if ref.user_id == new_user.id:
            logger.info("Self-referral ignored for user=%s.", new_user.id)
            return False
        if AmbassadorReferral.objects.filter(referred=new_user).exists():
            # referred is OneToOne — already attributed; keep the first.
            logger.info(
                "User %s already has a referral — kept first.", new_user.id
            )
            return False
        # Savepoint: a concurrent-signup IntegrityError must not poison a
        # caller's surrounding atomic block.
        with transaction.atomic():
            AmbassadorReferral.objects.create(
                referrer=ref.user, referred=new_user, code_used=ref.code
            )
        return True
    except IntegrityError:
        logger.info("User %s already has a referral — kept first.", new_user.id)
        return False
    except Exception:  # noqa: BLE001 — never break signup
        logger.exception("Referral attribution failed for code=%r.", code)
        return False


def complete_first_shift_if_referred(user):
    """Stamp the referred user's first completed shift; return the referral.

    Returns the freshly-stamped :class:`AmbassadorReferral` (so the caller
    can notify the referrer) or None when the user wasn't referred / was
    already stamped. The pending-row filter makes this idempotent across
    every later clock-out.
    """
    from ambassadors.models import AmbassadorReferral

    try:
        referral = (
            AmbassadorReferral.objects.filter(
                referred=user, first_shift_completed_at__isnull=True
            )
            .select_related("referrer", "referred")
            .first()
        )
        if referral is None:
            return None
        referral.first_shift_completed_at = timezone.now()
        referral.save(update_fields=["first_shift_completed_at", "updated_at"])
        return referral
    except Exception:  # noqa: BLE001 — never break clock-out
        logger.exception("Referral first-shift check failed for user=%s", user)
        return None
