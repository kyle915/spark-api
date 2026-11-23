# Audit trail when staff email a branded weekly / monthly client summary.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tenants", "0068_checkin_timesheet_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantweeklysummary",
            name="sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenantweeklysummary",
            name="sent_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="weekly_summaries_sent",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="tenantweeklysummary",
            name="sent_to",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
