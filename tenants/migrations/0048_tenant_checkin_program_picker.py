from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0047_brew_dr_insights_sales_series"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="checkin_program_picker",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
