from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recaps", "0031_client_notified_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="customrecap",
            name="is_third_party",
            field=models.BooleanField(default=False),
        ),
    ]
