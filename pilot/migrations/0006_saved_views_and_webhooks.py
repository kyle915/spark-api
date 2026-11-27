from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid6


class Migration(migrations.Migration):

    dependencies = [
        ("pilot", "0005_auto_match_receipt_uniqueness"),
        ("tenants", "0068_checkin_timesheet_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SavedReportView",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                (
                    "surface",
                    models.CharField(
                        choices=[
                            ("buyer_report", "Buyer Report"),
                            ("program_report", "Program Report"),
                            ("shelf_voice", "Shelf & Voice"),
                            ("retailer_pos", "Retailer POS"),
                        ],
                        db_index=True,
                        default="buyer_report",
                        max_length=32,
                    ),
                ),
                ("filters", models.JSONField(blank=True, default=dict)),
                (
                    "schedule_enabled",
                    models.BooleanField(db_index=True, default=False),
                ),
                (
                    "schedule_cadence",
                    models.CharField(
                        choices=[
                            ("weekly", "Weekly"),
                            ("biweekly", "Bi-weekly"),
                            ("monthly", "Monthly"),
                        ],
                        default="weekly",
                        max_length=16,
                    ),
                ),
                ("schedule_day_of_week", models.PositiveSmallIntegerField(default=4)),
                (
                    "export_format",
                    models.CharField(
                        choices=[
                            ("csv", "Summary + CSV"),
                            ("summary", "Summary Only"),
                        ],
                        default="csv",
                        max_length=16,
                    ),
                ),
                ("recipient_emails", models.TextField(blank=True, default="")),
                ("last_sent_at", models.DateTimeField(blank=True, null=True)),
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
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="saved_report_views_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="saved_report_views",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["name", "id"],
            },
        ),
        migrations.CreateModel(
            name="WebhookEndpoint",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                ("url", models.URLField(max_length=500)),
                (
                    "description",
                    models.CharField(blank=True, default="", max_length=240),
                ),
                ("events", models.JSONField(blank=True, default=list)),
                ("signing_secret", models.CharField(max_length=96)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("consecutive_failures", models.PositiveSmallIntegerField(default=0)),
                ("last_delivery_at", models.DateTimeField(blank=True, null=True)),
                (
                    "last_status_code",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="webhook_endpoints_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="webhook_endpoints",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="WebhookDelivery",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                ("event_type", models.CharField(db_index=True, max_length=48)),
                ("event_id", models.CharField(db_index=True, max_length=64)),
                ("payload", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[("delivered", "Delivered"), ("failed", "Failed")],
                        db_index=True,
                        default="failed",
                        max_length=16,
                    ),
                ),
                (
                    "http_status",
                    models.PositiveSmallIntegerField(blank=True, null=True),
                ),
                ("duration_ms", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "response_excerpt",
                    models.CharField(blank=True, default="", max_length=300),
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
                    "endpoint",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deliveries",
                        to="pilot.webhookendpoint",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="webhook_deliveries",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="savedreportview",
            index=models.Index(
                fields=["tenant", "surface"],
                name="pilot_savedview_ten_surf_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="savedreportview",
            index=models.Index(
                fields=["schedule_enabled", "schedule_day_of_week"],
                name="pilot_savedview_sched_day_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="webhookendpoint",
            index=models.Index(
                fields=["tenant", "is_active"],
                name="pilot_webhook_ten_act_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="webhookdelivery",
            index=models.Index(
                fields=["endpoint", "created_at"],
                name="pilot_whdeliv_ep_cr_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="webhookdelivery",
            index=models.Index(
                fields=["tenant", "created_at"],
                name="pilot_whdeliv_ten_cr_idx",
            ),
        ),
    ]
