"""Opt Torch into the Monday client weekly digest.

Ryan Heuser and the full sales org are coded in events.torch_retail_routing;
this flag is the cron gate. Dry-run of send-client-weekly-digest after the
by-state routing ship showed enabled=0 for every tenant.
"""

from django.db import migrations
from django.db.models import Q


TORCH_SLUGS = ("torch", "torch-thc", "keee-torch-thc")


def _enable_torch(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    Tenant.objects.filter(
        Q(slug__in=TORCH_SLUGS) | Q(request_url_name__in=TORCH_SLUGS)
    ).update(client_weekly_digest_enabled=True)


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0043_tenant_checkin_storage_units"),
    ]

    operations = [
        migrations.RunPython(_enable_torch, _noop),
    ]
