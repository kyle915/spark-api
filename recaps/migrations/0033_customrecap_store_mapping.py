from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recaps", "0032_customrecap_is_third_party"),
    ]

    operations = [
        migrations.AddField(
            model_name="customrecap",
            name="typed_store_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="typed_store_address",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="store_mapping_status",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="store_suggestions",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
