"""Per-tenant Monthly trend bars; Torch charts Consumers sampled + Units sold."""

from django.db import migrations, models
from django.db.models import Q

TORCH_SLUGS = ("torch", "torch-thc", "keee-torch-thc")


def _torch_sales(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(
        Q(slug__in=TORCH_SLUGS) | Q(request_url_name__in=TORCH_SLUGS)
    ).update(insights_trend_series="sales")


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0045_exclude_from_aggregates"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="insights_trend_series",
            field=models.CharField(
                choices=[
                    ("activity", "Engagements + Samples"),
                    ("sales", "Consumers sampled + Units sold"),
                ],
                default="activity",
                max_length=16,
            ),
        ),
        migrations.RunPython(_torch_sales, _noop),
    ]
