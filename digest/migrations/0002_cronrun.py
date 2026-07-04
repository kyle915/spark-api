from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("digest", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="CronRun",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("name", models.CharField(db_index=True, max_length=100, unique=True)),
                ("last_run_at", models.DateTimeField(blank=True, null=True)),
                ("last_status", models.PositiveSmallIntegerField(default=0)),
                ("last_ok", models.BooleanField(default=False)),
                ("last_detail", models.TextField(blank=True, default="")),
                ("run_count", models.PositiveIntegerField(default=0)),
            ],
            options={"ordering": ["name"]},
        ),
    ]
