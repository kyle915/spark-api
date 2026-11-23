# Program executive brief: one stored draft per brand and date range.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0069_weekly_summary_sent"),
    ]

    operations = [
        migrations.CreateModel(
            name="TenantProgramBrief",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("preset", models.CharField(default="custom", max_length=16)),
                ("paragraph", models.TextField()),
                ("bullets", models.JSONField(default=list)),
                ("quote", models.TextField(blank=True, default="")),
                ("has_edit", models.BooleanField(default=False)),
                ("edited_paragraph", models.TextField(blank=True, default="")),
                ("edited_bullets", models.JSONField(default=list)),
                ("edited_quote", models.TextField(blank=True, default="")),
                ("status", models.CharField(default="draft", max_length=16)),
                ("facts", models.JSONField(default=dict)),
                ("generated_at", models.DateTimeField()),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="program_briefs_reviewed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="program_briefs",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("tenant", "start_date", "end_date"),
                        name="tenants_program_brief_tenant_window_uniq",
                    )
                ],
            },
        ),
    ]
