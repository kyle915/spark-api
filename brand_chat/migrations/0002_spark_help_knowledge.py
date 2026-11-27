# Generated manually to match SparkHelpArticle and SparkHelpGap models cleanly.

import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brand_chat", "0001_initial"),
        ("tenants", "0066_ba_product_qa"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SparkHelpArticle",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("title", models.CharField(max_length=180)),
                ("slug", models.CharField(blank=True, db_index=True, default="", max_length=140)),
                ("category", models.CharField(db_index=True, default="general", max_length=60)),
                (
                    "audience",
                    models.CharField(
                        choices=[
                            ("all", "Clients & Admins"),
                            ("client", "Brand Clients"),
                            ("admin", "Ignite Admins Only"),
                        ],
                        db_index=True,
                        default="all",
                        max_length=16,
                    ),
                ),
                ("route_path", models.CharField(blank=True, default="", max_length=120)),
                ("route_label", models.CharField(blank=True, default="", max_length=80)),
                ("tags", models.JSONField(blank=True, default=list)),
                ("body", models.TextField()),
                (
                    "source_kind",
                    models.CharField(
                        choices=[
                            ("admin_faq", "Admin FAQ / SOP"),
                            ("chat_taught", "Taught from Chat"),
                            ("gap_resolved", "Resolved Knowledge Gap"),
                        ],
                        default="admin_faq",
                        max_length=24,
                    ),
                ),
                ("embedding", models.JSONField(blank=True, default=list)),
                ("embedding_model", models.CharField(blank=True, default="", max_length=64)),
                ("embedding_hash", models.CharField(blank=True, default="", max_length=64)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("usage_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="spark_help_articles_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="spark_help_articles",
                        to="tenants.tenant",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="spark_help_articles_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["is_active", "audience", "updated_at"],
                        name="spark_help_art_aud_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="SparkHelpGap",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("question", models.TextField()),
                ("normalized_key", models.CharField(db_index=True, max_length=200)),
                ("route_path", models.CharField(blank=True, default="", max_length=120)),
                ("caller_role", models.CharField(db_index=True, default="client", max_length=16)),
                ("source_surface", models.CharField(default="ask_spark", max_length=24)),
                ("reason", models.CharField(default="no_match", max_length=32)),
                ("asked_count", models.PositiveIntegerField(default=1)),
                ("last_asked_at", models.DateTimeField(auto_now=True, db_index=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("resolved", "Resolved"),
                            ("dismissed", "Dismissed"),
                        ],
                        db_index=True,
                        default="open",
                        max_length=16,
                    ),
                ),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "asked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="spark_help_gaps_asked",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "resolved_article",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="resolved_gaps",
                        to="brand_chat.sparkhelparticle",
                    ),
                ),
                (
                    "resolved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="spark_help_gaps_resolved",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="spark_help_gaps",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-asked_count", "-last_asked_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["status", "asked_count", "last_asked_at"],
                        name="spark_help_gap_queue_idx",
                    ),
                    models.Index(
                        fields=["normalized_key", "caller_role", "status"],
                        name="spark_help_gap_dedup_idx",
                    ),
                ],
            },
        ),
    ]
