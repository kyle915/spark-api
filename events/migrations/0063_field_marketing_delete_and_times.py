# Field marketing plans: soft delete (restorable) and start/end times.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0062_field_marketing_taxonomy"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="start_time",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="end_time",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="deleted_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="deleted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="field_marketing_events_deleted",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="delete_cancelled_request",
            field=models.BooleanField(default=False),
        ),
    ]
