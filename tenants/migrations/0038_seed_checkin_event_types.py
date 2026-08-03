"""Carry each tenant's single pinned check-in event type into the new list.

`Tenant.checkin_event_type` (0035) pinned ONE program per standing link.
`checkin_event_types` (0036) is the set a BA may choose between. A tenant that
already had a pin has exactly one program today, so its list starts as that one
entry — which keeps the page asking nothing (the selector needs two or more) and
leaves Total Wireless and Feel Free byte-identical.

Adding the second program is a deliberate act, done by
`setup_ld_retail_checkin`, not by this migration.
"""

from django.db import migrations


def seed_from_pin(apps, schema_editor):
    Tenant = apps.get_model("tenants", "Tenant")
    for tenant in Tenant.objects.exclude(checkin_event_type__isnull=True).only(
        "id", "checkin_event_type"
    ):
        tenant.checkin_event_types.add(tenant.checkin_event_type_id)


def unseed(apps, schema_editor):
    """Reverse by clearing the list — the pin it was copied from is untouched,
    so nothing is lost."""
    Tenant = apps.get_model("tenants", "Tenant")
    for tenant in Tenant.objects.only("id"):
        tenant.checkin_event_types.clear()


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0037_tenant_checkin_event_types"),
    ]

    operations = [
        migrations.RunPython(seed_from_pin, unseed),
    ]
