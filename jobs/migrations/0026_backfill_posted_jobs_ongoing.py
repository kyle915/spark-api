"""Backfill `ongoing=True` on already-posted jobs.

The BA job board (`my_available_jobs`) filters `ongoing=True, closed=False,
public=True`, but the post mutations (`post_job`, `post_event_to_board`)
historically set `public=True` + `lifecycle_status='posted'` without ever
flipping `ongoing` (which defaults to False) — so every posted job stayed
invisible on the board. The mutations are now fixed to set `ongoing=True`;
this repoints jobs that were posted before the fix so they don't need a
re-post to appear.

Scope: jobs that are POSTED and not closed. (Pending/filled/closed jobs are
intentionally left alone — they don't belong on the open board.)
"""

from django.db import migrations


def set_ongoing_on_posted(apps, schema_editor):
    Job = apps.get_model("jobs", "Job")
    Job.objects.filter(lifecycle_status="posted", closed=False).update(
        ongoing=True
    )


def noop_reverse(apps, schema_editor):
    # No safe reverse — we can't know which jobs were ongoing before the
    # backfill. Leaving the flag set is harmless.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0025_seed_global_contractor_agreement"),
    ]

    operations = [
        migrations.RunPython(set_ongoing_on_posted, noop_reverse),
    ]
