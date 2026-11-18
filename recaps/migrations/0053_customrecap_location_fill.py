from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recaps", "0052_shelf_reading"),
    ]

    operations = [
        migrations.AddField(
            model_name="customrecap",
            name="store_number",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="location_fill",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
