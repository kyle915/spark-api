from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0046_shift_sms'),
    ]

    operations = [
        migrations.AddField(
            model_name='shiftsms',
            name='attempts',
            field=models.PositiveSmallIntegerField(default=1),
        ),
    ]
