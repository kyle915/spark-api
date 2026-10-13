from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("jobboard", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobboardgig",
            name="public_notes",
            field=models.TextField(blank=True, default=""),
        ),
    ]
