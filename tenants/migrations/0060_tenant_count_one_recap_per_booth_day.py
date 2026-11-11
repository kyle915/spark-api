from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0059_tenant_program_share_controls'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='count_one_recap_per_booth_day',
            field=models.BooleanField(default=False),
        ),
    ]
