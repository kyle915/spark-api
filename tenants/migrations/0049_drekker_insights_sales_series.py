"""Drekker Brewing Insights on the sales metric set (Consumers sampled + Units sold)."""

from django.db import migrations

DREKKER = "drekker-brewing"


def _set_series(series):
    def run(apps, schema_editor):
        Tenant = apps.get_model("tenants", "Tenant")
        tenant = (
            Tenant.objects.filter(slug=DREKKER).order_by("id").first()
            or Tenant.objects.filter(request_url_name=DREKKER).order_by("id").first()
        )
        if tenant is not None:
            Tenant.objects.filter(id=tenant.id).update(insights_trend_series=series)

    return run


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0048_tenant_checkin_program_picker"),
    ]

    operations = [
        migrations.RunPython(_set_series("sales"), _set_series("activity")),
    ]
