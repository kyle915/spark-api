"""Backfill `is_approved=True` on bookings whose job assignment is approved.

`create_ambassador_job` (admin direct-assign) historically created the
AmbassadorEvent booking with `is_approved=False` even though it sets the
AmbassadorJob to status=approved. The BA's upcoming-shifts + clock-in both
require `AmbassadorEvent.is_approved=True`, so every BA hired this way was
booked on paper but couldn't see or clock into the shift. The mutation is
now fixed to create the booking approved; this repairs the ones already
created wrong.

Scoped tightly to the bug's signature — only bookings that have a matching
APPROVED AmbassadorJob (same ambassador + event) are flipped. Legitimate
unapproved bookings (pending event/shift-offer invites the BA hasn't
accepted) have no approved AmbassadorJob, so they're left alone.
"""

from django.db import migrations
from django.db.models import Exists, OuterRef


def approve_bookings_with_approved_job(apps, schema_editor):
    AmbassadorEvent = apps.get_model("ambassadors", "AmbassadorEvent")
    AmbassadorJob = apps.get_model("jobs", "AmbassadorJob")

    approved_job = AmbassadorJob.objects.filter(
        ambassador_id=OuterRef("ambassador_id"),
        job__event_id=OuterRef("event_id"),
        status__slug="approved",
    )
    (
        AmbassadorEvent.objects.filter(is_approved=False)
        .filter(Exists(approved_job))
        .update(is_approved=True)
    )


def noop_reverse(apps, schema_editor):
    # Can't know which were unapproved before; leaving them approved is
    # harmless (they have an approved job assignment).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ambassadors", "0031_ambassadorevent_confirmation_requested_at_and_more"),
        ("jobs", "0026_backfill_posted_jobs_ongoing"),
    ]

    operations = [
        migrations.RunPython(approve_bookings_with_approved_job, noop_reverse),
    ]
