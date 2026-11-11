from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0058_tenant_program_share_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='program_share_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenant',
            name='program_share_last_viewed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='tenant',
            name='program_share_views',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
