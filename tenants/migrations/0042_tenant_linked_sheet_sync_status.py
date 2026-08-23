from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0041_tenant_checkin_recap_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="linked_sheet_last_sync_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenant",
            name="linked_sheet_last_sync_error",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenant",
            name="linked_sheet_last_request_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]
