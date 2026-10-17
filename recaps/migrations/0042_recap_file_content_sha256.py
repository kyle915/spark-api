from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recaps", "0041_customrecap_co_activation_key_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="recapfile",
            name="content_sha256",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="customrecapfile",
            name="content_sha256",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
    ]
