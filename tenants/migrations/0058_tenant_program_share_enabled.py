from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0057_notification_preference'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='program_share_enabled',
            field=models.BooleanField(default=False),
        ),
    ]
