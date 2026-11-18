# BA product knowledge: extracted text from brand documents + website.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("tenants", "0066_ba_product_qa"),
    ]

    operations = [
        migrations.CreateModel(
            name="KnowledgeRefresh",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("scope", models.CharField(max_length=12)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("ok", models.BooleanField(default=False)),
                ("stats", models.JSONField(blank=True, default=dict)),
                ("message", models.CharField(blank=True, default="", max_length=300)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="knowledge_refreshes",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["-started_at"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "scope", "-started_at"],
                        name="product_kno_tenant__742694_idx",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="KnowledgeSource",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("website_page", "Website page"),
                            ("shopify_product", "Website product"),
                            ("training_doc", "Training hub document"),
                            ("checkin_resource", "Check-in resource"),
                            ("briefing", "BA briefing"),
                            ("briefing_attachment", "BA briefing attachment"),
                        ],
                        max_length=24,
                    ),
                ),
                ("ref", models.CharField(max_length=600)),
                ("url", models.CharField(blank=True, default="", max_length=1024)),
                ("title", models.CharField(blank=True, default="", max_length=300)),
                ("text", models.TextField(blank=True, default="")),
                (
                    "content_hash",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("raw_hash", models.CharField(blank=True, default="", max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ok", "Text extracted"),
                            ("empty", "No readable text"),
                            ("error", "Could not fetch or read"),
                        ],
                        default="ok",
                        max_length=10,
                    ),
                ),
                ("error", models.CharField(blank=True, default="", max_length=300)),
                ("excluded", models.BooleanField(default=False)),
                ("extracted_at", models.DateTimeField(blank=True, null=True)),
                ("checked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="knowledge_sources",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ["kind", "title", "id"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "excluded", "status"],
                        name="product_kno_tenant__fbdcc6_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "ref"), name="knowledge_source_tenant_ref"
                    )
                ],
            },
        ),
    ]
