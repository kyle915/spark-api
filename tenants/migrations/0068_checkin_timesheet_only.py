# Time-sheet-only standing check-in (clock in/out, no recap).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0067_brand_website_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='checkin_timesheet_only',
            field=models.BooleanField(default=False),
        ),
    ]
