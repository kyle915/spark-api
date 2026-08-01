"""Remove a junk walk-in event created through a standing check-in link.

The standing tenant link (see `project_tw_standing_checkin`) lets any BA
conjure an Event by typing a store address. That is the point of it — but it
means typos, test taps and abandoned check-ins all become real Event rows, and
those rows are NOT harmless: `recent_checkin_locations()` feeds them straight
back to every other BA as a store suggestion. One fat-fingered address becomes
a permanent wrong answer in the field team's autocomplete.

So the feature needs a matching removal, and this is it.

GUARDS — every one of these REFUSES rather than deletes, because the whole
point is that this can never eat a real activation:

  * NAME MATCH. `--expect-name` must appear in the event's name. Deleting by
    id alone is how you remove the wrong row; this is the same content guard
    that backed out the LD orphan-row mistake.
  * NO RECAP. If anything has been filed against the event, stop. That is
    real field work.
  * NO APPROVED BOOKING. A confirmed BA means ops already blessed it.
  * NO CLOCK TIME. Any Attendance row means somebody actually worked it.
  * TENANT REQUIRED. Refuses on an event with no tenant, which would be a
    sign the uuid isn't what the caller thinks.

What it removes: the pending AmbassadorEvent booking(s), any LocationPing
rows, and the Event itself.

What it deliberately does NOT touch: the walk-up Ambassador/User stub. Those
are soft-deactivated in this codebase, never hard-deleted (RESTRICT FKs — see
`reference_user_ba_removal`), and the stub is inert anyway: phone-keyed, not a
real account, and reusable if that person ever checks in for real.

Dry-run by default.

Usage:
    python manage.py purge_walkin_event --event-uuid <uuid> --expect-name "ZZ Spark"
    python manage.py purge_walkin_event --event-uuid <uuid> --expect-name "ZZ Spark" --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Delete a junk walk-in event (typo/test) created via a standing "
        "check-in link. Refuses if it carries any real work. Dry-run default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--event-uuid", type=str, required=True)
        parser.add_argument(
            "--expect-name", type=str, required=True,
            help=(
                "Substring that MUST appear in the event's name. Content "
                "guard — deleting by id alone is how the wrong row goes."
            ),
        )
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        from ambassadors.models import AmbassadorEvent, Attendance, LocationPing
        from events.models import Event
        from recaps.models import CustomRecap

        apply = opts["apply"]
        uuid_s = (opts["event_uuid"] or "").strip()
        expect = (opts["expect_name"] or "").strip()
        if not expect:
            raise CommandError("--expect-name may not be blank.")

        if not apply:
            self.stdout.write(self.style.WARNING(
                "DRY RUN — pass --apply to actually delete.\n"
            ))

        event = (
            Event.objects.select_related("tenant").filter(uuid=uuid_s).first()
        )
        if event is None:
            raise CommandError(f"No event with uuid {uuid_s!r}.")

        name = event.name or ""
        self.stdout.write(
            f"event   : {name!r}\n"
            f"uuid    : {event.uuid}\n"
            f"tenant  : {getattr(event.tenant, 'name', None)!r}\n"
            f"address : {getattr(event, 'address', None)!r}\n"
            f"date    : {event.date}"
        )

        # --- guards -------------------------------------------------------
        if event.tenant_id is None:
            raise CommandError("REFUSED: event has no tenant.")
        if expect.lower() not in name.lower():
            raise CommandError(
                f"REFUSED: name {name!r} does not contain {expect!r}. "
                "This is not the row you think it is."
            )

        recaps = CustomRecap.objects.filter(event_id=event.id).count()
        if recaps:
            raise CommandError(
                f"REFUSED: {recaps} recap(s) filed against this event — "
                "that is real field work."
            )

        punches = Attendance.objects.filter(event_id=event.id).count()
        if punches:
            raise CommandError(
                f"REFUSED: {punches} clock punch(es) on this event — "
                "somebody actually worked it."
            )

        bookings = list(
            AmbassadorEvent.objects.filter(event_id=event.id)
            .select_related("ambassador__user")
        )
        approved = [b for b in bookings if getattr(b, "is_approved", False)]
        if approved:
            raise CommandError(
                f"REFUSED: {len(approved)} APPROVED booking(s) — ops already "
                "confirmed this activation."
            )

        pings = LocationPing.objects.filter(event_id=event.id).count()
        self.stdout.write(
            f"\nwould remove: {len(bookings)} pending booking(s), "
            f"{pings} location ping(s), 1 event"
        )
        for b in bookings:
            who = getattr(getattr(b, "ambassador", None), "user", None)
            label = getattr(who, "first_name", "") or getattr(who, "email", "?")
            self.stdout.write(f"  - pending booking: {label}")
        self.stdout.write(
            "  (the walk-up BA stub is left alone — soft-deactivate only)"
        )

        if not apply:
            self.stdout.write("\nAll guards pass. Re-run with --apply.")
            return

        with transaction.atomic():
            LocationPing.objects.filter(event_id=event.id).delete()
            AmbassadorEvent.objects.filter(event_id=event.id).delete()
            event_id = event.id
            event.delete()

        self.stdout.write(self.style.SUCCESS(
            f"\nDeleted event {event_id} ({name!r}) and its pending bookings."
        ))
        self.stdout.write(
            f"JSON_RESULT:{{\"deleted\": true, \"event\": \"{uuid_s}\"}}"
        )
