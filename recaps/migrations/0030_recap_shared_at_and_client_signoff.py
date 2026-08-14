from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recaps", "0029_recap_submitted_at_alias"),
    ]

    operations = [
        migrations.AddField(
            model_name="recap",
            name="shared_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="recap",
            name="client_signoff_status",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="recap",
            name="client_signoff_comment",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="recap",
            name="client_signoff_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="shared_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="client_signoff_status",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="client_signoff_comment",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="client_signoff_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
