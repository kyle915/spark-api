# Generated manually for SPA-32 automated POS feeds + weekly buyer digests.

import uuid6
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("pilot", "0001_initial"),
        ("tenants", "0068_checkin_timesheet_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspaceprofile",
            name="buyer_weekly_digest_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When ON, this brand receives the Monday weekly buyer "
                    "store-report email (CSV attached). OFF by default — "
                    "opt-in only."
                ),
            ),
        ),
        migrations.AddField(
            model_name="workspaceprofile",
            name="buyer_digest_recipient_emails",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Optional override recipients for the weekly buyer digest "
                    "(comma/newline separated). When blank, brand_admin + "
                    "finance_reviewer membership emails are used."
                ),
            ),
        ),
        migrations.CreateModel(
            name="PosFeedConfig",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                (
                    "retailer_banner",
                    models.CharField(db_index=True, max_length=150),
                ),
                (
                    "preset_key",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "source_kind",
                    models.CharField(
                        choices=[
                            ("email_drop", "Email / Webhook Drop"),
                            ("manual_upload", "Manual Upload Assist"),
                            ("sftp", "Scheduled SFTP / Connector"),
                        ],
                        default="email_drop",
                        max_length=24,
                    ),
                ),
                (
                    "ingest_token",
                    models.CharField(db_index=True, max_length=64, unique=True),
                ),
                ("enabled", models.BooleanField(db_index=True, default=True)),
                (
                    "default_feed_completeness",
                    models.CharField(default="complete", max_length=16),
                ),
                ("notes", models.TextField(blank=True, default="")),
                (
                    "last_ingested_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, editable=False),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pos_feed_configs_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pos_feed_configs",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["retailer_banner", "id"],
            },
        ),
        migrations.CreateModel(
            name="PosFeedDrop",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processed", "Processed"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                (
                    "filename",
                    models.CharField(blank=True, default="pos.csv", max_length=255),
                ),
                ("csv_text", models.TextField()),
                (
                    "file_sha256",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=64
                    ),
                ),
                (
                    "source_from_email",
                    models.EmailField(blank=True, default="", max_length=254),
                ),
                (
                    "source_message_id",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("error_message", models.TextField(blank=True, default="")),
                (
                    "received_at",
                    models.DateTimeField(auto_now_add=True, editable=False),
                ),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "feed_config",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="drops",
                        to="pilot.posfeedconfig",
                    ),
                ),
                (
                    "sales_import",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="feed_drops",
                        to="pilot.retailersalesimport",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pos_feed_drops",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-received_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="posfeedconfig",
            index=models.Index(
                fields=["tenant", "enabled"],
                name="pilot_posfeed_ten_en_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="posfeedconfig",
            constraint=models.UniqueConstraint(
                fields=("tenant", "retailer_banner", "preset_key"),
                name="pilot_posfeed_tenant_banner_preset_uniq",
            ),
        ),
        migrations.AddIndex(
            model_name="posfeeddrop",
            index=models.Index(
                fields=["tenant", "status", "received_at"],
                name="pilot_posdrop_ten_st_rec_idx",
            ),
        ),
    ]
