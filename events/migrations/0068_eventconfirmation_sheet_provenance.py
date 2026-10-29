from django.db import migrations, models


def _backfill_sheet_source(apps, schema_editor):
    """Sheet automation always left created_by null; admin-tab sets a user."""
    EventConfirmation = apps.get_model("events", "EventConfirmation")
    EventConfirmation.objects.filter(created_by__isnull=True).update(source="sheet")


def _noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0067_eventconfirmation_brand_slugs"),
    ]

    operations = [
        migrations.AddField(
            model_name="eventconfirmation",
            name="source",
            field=models.CharField(
                choices=[("admin", "Admin tab"), ("sheet", "Sheet automation")],
                db_index=True,
                default="admin",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="eventconfirmation",
            name="sheet_id",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="eventconfirmation",
            name="sheet_key",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="eventconfirmation",
            name="sheet_row",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="eventconfirmation",
            name="sheet_stamp_status",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="eventconfirmation",
            name="sheet_stamped_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(_backfill_sheet_source, _noop_reverse),
    ]
