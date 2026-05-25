# Generated for spark-mobile push notification support.

import uuid6
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ambassadors", "0018_ambassadorgroupjob"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PushDevice",
            fields=[
                (
                    "id",
                    models.BigAutoField(primary_key=True, serialize=False),
                ),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                (
                    "token",
                    models.CharField(db_index=True, max_length=255, unique=True),
                ),
                (
                    "platform",
                    models.CharField(
                        choices=[
                            ("ios", "iOS"),
                            ("android", "Android"),
                            ("web", "Web"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "device_name",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                (
                    "app_version",
                    models.CharField(blank=True, max_length=40, null=True),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="push_devices",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="pushdevice",
            index=models.Index(
                fields=["user", "is_active"], name="ambassadors_user_id_193b88_idx"
            ),
        ),
    ]
