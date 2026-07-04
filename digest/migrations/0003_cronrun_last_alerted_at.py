from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("digest", "0002_cronrun"),
    ]

    operations = [
        migrations.AddField(
            model_name="cronrun",
            name="last_alerted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
