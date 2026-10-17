import django.db.models.deletion
import uuid6
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recaps", "0041_customrecap_co_activation_key_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="RecapComment",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("uuid", models.UUIDField(default=uuid6.uuid7, editable=False, unique=True)),
                ("author_name", models.CharField(blank=True, default="", max_length=120)),
                ("author_email", models.CharField(blank=True, default="", max_length=254)),
                ("body", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "custom_recap",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="recaps.customrecap",
                    ),
                ),
                (
                    "recap",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="recaps.recap",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("custom_recap__isnull", True), ("recap__isnull", False)),
                            models.Q(("custom_recap__isnull", False), ("recap__isnull", True)),
                            _connector="OR",
                        ),
                        name="recap_comment_one_recap",
                    )
                ],
            },
        ),
    ]
