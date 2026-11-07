from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0054_tenant_weekly_narration"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="retail_sheet_id",
            field=models.CharField(
                blank=True, db_index=True, max_length=128, null=True
            ),
        ),
    ]
