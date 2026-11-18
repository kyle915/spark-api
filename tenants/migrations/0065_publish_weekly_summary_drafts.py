"""Weekly summaries are now published to the brand when written (see
recaps.weekly_summary.generate_summary_draft). Publish the weekly drafts
already stored so every brand's dashboard catches up. Monthly email drafts
are left alone. Reversible only as a no-op: nothing records which rows this
touched beyond reviewed_by being empty."""

from django.db import migrations
from django.utils import timezone


def publish(apps, schema_editor):
    Row = apps.get_model("tenants", "TenantWeeklySummary")
    Row.objects.filter(period="week", status="draft").update(
        status="reviewed", reviewed_at=timezone.now(), reviewed_by=None
    )


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0064_comment_insight"),
    ]

    operations = [migrations.RunPython(publish, migrations.RunPython.noop)]
