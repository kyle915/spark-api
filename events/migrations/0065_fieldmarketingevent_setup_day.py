from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0064_event_co_activation_host_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="setup_day",
            field=models.BooleanField(default=False),
        ),
    ]
