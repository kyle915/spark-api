from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recaps", "0020_customfieldvalue_customrecapfile"),
    ]

    operations = [
        migrations.AddField(
            model_name="recap",
            name="external_ba_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
