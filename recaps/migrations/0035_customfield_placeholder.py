from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recaps", "0034_recap_approved_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="customfield",
            name="placeholder",
            field=models.TextField(blank=True, default=""),
        ),
    ]
