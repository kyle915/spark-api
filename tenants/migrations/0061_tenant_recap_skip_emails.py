from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0060_tenant_count_one_recap_per_booth_day'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='recap_skip_emails',
            field=models.TextField(blank=True, default='', help_text="Emails (comma/newline/semicolon-separated) that never receive this brand's approved-recap email, even if they are a client user, RMM or requestor on it."),
        ),
    ]
