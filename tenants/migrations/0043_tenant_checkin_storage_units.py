from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0042_tenant_linked_sheet_sync_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='checkin_storage_units',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
