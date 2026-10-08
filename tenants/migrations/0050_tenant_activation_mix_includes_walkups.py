"""Brew Dr's activation mix counts its approved walk-up demos (it schedules no Requests)."""
from django.db import migrations, models

BREW_DR = "brew-dr-kombucha"


def _set_flag(value):
    def run(apps, schema_editor):
        Tenant = apps.get_model("tenants", "Tenant")
        tenant = (
            Tenant.objects.filter(slug=BREW_DR).order_by("id").first()
            or Tenant.objects.filter(request_url_name=BREW_DR).order_by("id").first()
        )
        if tenant is not None:
            Tenant.objects.filter(id=tenant.id).update(
                activation_mix_includes_walkups=value
            )

    return run


class Migration(migrations.Migration):
    dependencies = [("tenants", "0049_drekker_insights_sales_series")]
    operations = [
        migrations.AddField(
            model_name="tenant",
            name="activation_mix_includes_walkups",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(_set_flag(True), _set_flag(False)),
    ]
