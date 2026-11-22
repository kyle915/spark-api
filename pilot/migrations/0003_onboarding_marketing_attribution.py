from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pilot", "0002_milestone2_subscriptions"),
    ]

    operations = [
        migrations.AddField(
            model_name="brandonboardingprogress",
            name="marketing_attribution",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="brandonboardingprogress",
            name="close_lead_id",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
    ]
