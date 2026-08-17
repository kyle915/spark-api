from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recaps", "0030_recap_shared_at_and_client_signoff"),
    ]

    operations = [
        migrations.AddField(
            model_name="recap",
            name="client_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="client_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
