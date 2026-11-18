from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recaps", "0052_shelf_reading"),
    ]

    operations = [
        migrations.AddField(
            model_name="receiptreading",
            name="store_number",
            field=models.CharField(blank=True, default="", max_length=8),
        ),
        migrations.AddField(
            model_name="receiptreading",
            name="charges",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="receiptreading",
            name="reader_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
    ]
