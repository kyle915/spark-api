from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0035_tenant_checkin_event_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='checkin_photo_buckets',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
