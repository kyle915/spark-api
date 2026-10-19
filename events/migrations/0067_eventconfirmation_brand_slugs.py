from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0066_request_ba_count"),
    ]

    operations = [
        migrations.AddField(
            model_name="eventconfirmation",
            name="brand_slugs",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
