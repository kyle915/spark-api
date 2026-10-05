"""Brew Dr. Kombucha Insights on the sales metric set (Consumers sampled + Units sold)."""

from django.db import migrations

BREW_DR = "brew-dr-kombucha"


def _set_series(series):
    def run(apps, schema_editor):
        Tenant = apps.get_model("tenants", "Tenant")
        tenant = (
            Tenant.objects.filter(slug=BREW_DR).order_by("id").first()
            or Tenant.objects.filter(request_url_name=BREW_DR).order_by("id").first()
        )
        if tenant is not None:
            Tenant.objects.filter(id=tenant.id).update(insights_trend_series=series)

    return run


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0046_tenant_insights_trend_series"),
    ]

    operations = [
        migrations.RunPython(_set_series("sales"), _set_series("activity")),
    ]
