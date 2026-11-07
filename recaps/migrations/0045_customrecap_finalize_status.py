from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recaps", "0044_merge_recap_leaves"),
    ]

    operations = [
        migrations.AddField(
            model_name="customrecap",
            name="finalize_status",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="finalize_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="customrecap",
            name="finalize_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
