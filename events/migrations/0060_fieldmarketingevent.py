# Field marketing plans, separate from retail sampling requests.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import uuid6


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0059_activationplan_request_activation_plan"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FieldMarketingEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(default=uuid6.uuid7, editable=False, unique=True),
                ),
                ("market", models.CharField(max_length=32)),
                (
                    "activity",
                    models.CharField(
                        choices=[
                            ("full_can", "Full can samples"),
                            ("pour", "4oz pour samples"),
                            ("sponsorship", "Local event sponsorship"),
                            ("retail_support", "Retail activation / support"),
                        ],
                        max_length=32,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("starts_on", models.DateField()),
                ("days", models.PositiveIntegerField(default=1)),
                ("address", models.TextField(blank=True, default="")),
                ("notes", models.TextField(blank=True, default="")),
                ("planned_full_cans", models.PositiveIntegerField(default=0)),
                ("planned_pour_samples", models.PositiveIntegerField(default=0)),
                ("planned_emails", models.PositiveIntegerField(default=0)),
                ("logged_full_cans", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "logged_pour_samples",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                ("logged_emails", models.PositiveIntegerField(blank=True, null=True)),
                ("logged_days", models.PositiveIntegerField(blank=True, null=True)),
                ("logged_at", models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("planned", "Planned"), ("submitted", "Submitted")],
                        default="planned",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="field_marketing_events_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="field_marketing_events",
                        to="events.request",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="field_marketing_events",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ("starts_on", "id"),
            },
        ),
        migrations.AddIndex(
            model_name="fieldmarketingevent",
            index=models.Index(
                fields=["tenant", "starts_on"], name="ev_fm_tenant_date_idx"
            ),
        ),
    ]
