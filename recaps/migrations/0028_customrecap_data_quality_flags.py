from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recaps", "0027_seed_select_field_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="customrecap",
            name="data_quality_flags",
            field=models.TextField(blank=True, default=""),
        ),
    ]
