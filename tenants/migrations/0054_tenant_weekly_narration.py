from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0053_tenant_request_auto_approve"),
    ]

    operations = [
        migrations.CreateModel(
            name="TenantWeeklyNarration",
            fields=[
                (
                    "id",
                    models.BigAutoField(primary_key=True, serialize=False),
                ),
                ("week_start", models.DateField()),
                ("week_end", models.DateField()),
                ("body", models.TextField()),
                ("facts", models.JSONField(default=dict)),
                ("filed_recaps", models.IntegerField(default=0)),
                ("source", models.CharField(default="fallback", max_length=16)),
                ("generated_at", models.DateTimeField(auto_now=True)),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="weekly_narrations",
                        to="tenants.tenant",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="tenantweeklynarration",
            constraint=models.UniqueConstraint(
                fields=("tenant", "week_start"),
                name="tenants_weekly_narration_tenant_week_uniq",
            ),
        ),
    ]
