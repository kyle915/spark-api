# Generated manually for store-manager phone overflow on public spark-form.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0060_fieldmarketingevent"),
    ]

    operations = [
        migrations.AlterField(
            model_name="request",
            name="store_manager_phone",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="requeststoremanager",
            name="phone",
            field=models.CharField(max_length=64),
        ),
    ]
