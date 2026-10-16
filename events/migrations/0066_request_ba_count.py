from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0065_fieldmarketingevent_setup_day"),
    ]

    operations = [
        migrations.AddField(
            model_name="request",
            name="ba_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
