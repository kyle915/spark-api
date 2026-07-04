from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ambassadors", "0037_shiftextensionrequest_decision_reason"),
    ]

    operations = [
        migrations.AddField(
            model_name="ambassadorevent",
            name="activation_nudge_stage",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
