# Generated manually for DemoBudgetTarget (SPA-37).

import decimal

import django.db.models.deletion
import uuid6
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brand_insights", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DemoBudgetTarget",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                ("month", models.DateField(db_index=True)),
                (
                    "scope_type",
                    models.CharField(
                        choices=[
                            ("workspace", "Workspace"),
                            ("retailer", "Retailer"),
                            ("market", "Market"),
                        ],
                        default="workspace",
                        max_length=16,
                    ),
                ),
                (
                    "scope_key",
                    models.CharField(blank=True, default="", max_length=200),
                ),
                (
                    "scope_label",
                    models.CharField(blank=True, default="", max_length=200),
                ),
                (
                    "target_amount",
                    models.DecimalField(
                        decimal_places=2,
                        default=decimal.Decimal("0.00"),
                        max_digits=12,
                    ),
                ),
                ("notes", models.TextField(blank=True, default="")),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, editable=False),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="demo_budget_targets",
                        to="tenants.tenant",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="demo_budget_targets_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["tenant", "month"],
                        name="brand_demo_budget_month_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "month", "scope_type", "scope_key"),
                        name="brand_demo_budget_target_uniq",
                    )
                ],
            },
        ),
    ]
