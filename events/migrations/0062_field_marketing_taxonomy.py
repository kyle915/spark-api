# Field marketing activities from the Torch client call:
# event activation, guerilla, product seeding, and sales support.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0061_widen_store_manager_phone"),
    ]

    operations = [
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="sampling_format",
            field=models.CharField(
                blank=True,
                choices=[("", ""), ("full_can", "Full can"), ("pour", "4oz pour")],
                default="",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="support_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", ""),
                    ("distributor_meeting", "Distributor meeting"),
                    ("retail_visit", "Retail visit"),
                    ("other", "Other"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="support_other",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="sku_names",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="needs_field_support",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="ambassador_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="support_times",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="support_scope",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="fieldmarketingevent",
            name="logged_cases",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="fieldmarketingevent",
            name="activity",
            field=models.CharField(
                choices=[
                    ("full_can", "Full can samples"),
                    ("pour", "4oz pour samples"),
                    ("sponsorship", "Local event sponsorship"),
                    ("retail_support", "Retail activation / support"),
                    ("event_activation", "Event activation / sponsorship"),
                    ("guerilla", "Guerilla event"),
                    ("product_seeding", "Product seeding"),
                    ("sales_support", "Sales support"),
                ],
                max_length=32,
            ),
        ),
    ]
