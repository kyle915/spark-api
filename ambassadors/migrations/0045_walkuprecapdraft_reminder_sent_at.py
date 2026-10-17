from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ambassadors", "0044_walkup_recap_draft"),
    ]

    operations = [
        migrations.AddField(
            model_name="walkuprecapdraft",
            name="reminder_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
