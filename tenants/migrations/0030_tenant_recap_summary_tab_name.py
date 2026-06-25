from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0029_tenant_recap_export_on_submit_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='recap_summary_tab_name',
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
    ]
