from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0052_brandgroup_tenant_brand_group_brandgroupaccess"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="auto_approve_requests",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="tenant",
            name="request_approved_cc_emails",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Emails (comma/newline/semicolon-separated) CC'd on this brand's auto-approved request email.",
            ),
        ),
    ]
