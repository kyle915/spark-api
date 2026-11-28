from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid6


class Migration(migrations.Migration):

    dependencies = [
        ("pilot", "0006_saved_views_and_webhooks"),
        ("tenants", "0068_checkin_timesheet_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ExecutiveSnapshotConfig",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("enabled", models.BooleanField(db_index=True, default=False)),
                ("recipient_emails", models.TextField(blank=True, default="")),
                (
                    "target_msrp",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=8, null=True
                    ),
                ),
                ("include_photos", models.BooleanField(default=True)),
                ("last_sent_at", models.DateTimeField(blank=True, null=True)),
                (
                    "last_sent_month",
                    models.CharField(blank=True, default="", max_length=7),
                ),
                (
                    "last_send_status",
                    models.CharField(blank=True, default="", max_length=32),
                ),
                (
                    "last_send_error",
                    models.CharField(blank=True, default="", max_length=240),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="executive_snapshot_config",
                        to="tenants.tenant",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="executive_snapshot_configs_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["tenant_id"],
            },
        ),
        migrations.CreateModel(
            name="RegionalAlertSubscription",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("states", models.JSONField(blank=True, default=list)),
                ("chains", models.JSONField(blank=True, default=list)),
                ("alert_oos", models.BooleanField(default=True)),
                ("alert_price_drift", models.BooleanField(default=True)),
                ("alert_missing_tag", models.BooleanField(default=True)),
                (
                    "target_msrp",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=8, null=True
                    ),
                ),
                (
                    "drift_threshold_pct",
                    models.DecimalField(
                        decimal_places=1, default=Decimal("5.0"), max_digits=5
                    ),
                ),
                ("notify_email", models.BooleanField(default=True)),
                ("recipient_emails", models.TextField(blank=True, default="")),
                ("notify_webhook", models.BooleanField(default=False)),
                (
                    "webhook_url",
                    models.URLField(blank=True, default="", max_length=500),
                ),
                (
                    "webhook_secret",
                    models.CharField(blank=True, default="", max_length=96),
                ),
                ("last_triggered_at", models.DateTimeField(blank=True, null=True)),
                ("trigger_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="regional_alert_subscriptions_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="regional_alert_subscriptions",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="RegionalAlertDelivery",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                (
                    "trigger_kind",
                    models.CharField(
                        choices=[
                            ("oos", "Out of Stock"),
                            ("price_drift", "MSRP Price Drift"),
                            ("missing_tag", "Missing Price Tag"),
                            ("test", "Test Alert"),
                        ],
                        db_index=True,
                        max_length=24,
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[("email", "Email"), ("webhook", "Webhook")],
                        default="email",
                        max_length=16,
                    ),
                ),
                (
                    "store_name",
                    models.CharField(blank=True, default="", max_length=160),
                ),
                (
                    "state_code",
                    models.CharField(blank=True, default="", max_length=12),
                ),
                (
                    "summary",
                    models.CharField(blank=True, default="", max_length=320),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("sent", "Sent"), ("failed", "Failed")],
                        db_index=True,
                        default="failed",
                        max_length=16,
                    ),
                ),
                (
                    "error_message",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                (
                    "subscription",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deliveries",
                        to="pilot.regionalalertsubscription",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="regional_alert_deliveries",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="regionalalertsubscription",
            index=models.Index(
                fields=["tenant", "is_active"],
                name="pilot_regalert_ten_act_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="regionalalertdelivery",
            index=models.Index(
                fields=["subscription", "created_at"],
                name="pilot_regdeliv_sub_cr_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="regionalalertdelivery",
            index=models.Index(
                fields=["tenant", "created_at"],
                name="pilot_regdeliv_ten_cr_idx",
            ),
        ),
    ]
