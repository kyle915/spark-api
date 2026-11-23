# Shared Today's attention discard (SPA-69).

import django.db.models.deletion
import uuid6
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brand_insights", "0003_buyer_pitch_deck_and_photo_wall_pin"),
        ("tenants", "0068_checkin_timesheet_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AttentionDismissal",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                (
                    "bucket",
                    models.CharField(
                        choices=[
                            ("receipt_checks", "Receipt checks"),
                            ("missing_recaps", "Missing recaps"),
                            ("unstaffed", "Unstaffed (48h)"),
                            ("rebooks", "Rebook stores"),
                        ],
                        db_index=True,
                        max_length=32,
                    ),
                ),
                ("ref_id", models.CharField(max_length=64)),
                ("fingerprint", models.CharField(max_length=100)),
                (
                    "dismissed_at",
                    models.DateTimeField(auto_now_add=True, editable=False),
                ),
                ("restored_at", models.DateTimeField(blank=True, null=True)),
                (
                    "dismissed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="attention_dismissals_made",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "restored_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="attention_dismissals_restored",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attention_dismissals",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["tenant", "bucket", "restored_at"],
                        name="attention_dismissal_lookup_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("restored_at__isnull", True)),
                        fields=("tenant", "fingerprint"),
                        name="attention_dismissal_active_fp_uniq",
                    )
                ],
            },
        ),
    ]
