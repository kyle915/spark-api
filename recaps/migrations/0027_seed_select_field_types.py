"""Seed the global 'select' and 'multiselect' CustomRecapFieldType rows.

These are the new dropdown / multi-select field types for custom recap
templates. Field types are GLOBAL (no tenant FK), like the existing
text/number/image/longtext types, so one row each serves every tenant. The
template builder lists CustomRecapFieldType rows in its type picker, so these
must exist for "Dropdown" / "Multi-select" to be selectable.

Idempotent (get_or_create) and a no-op on a fresh/empty DB (no user to own the
row yet — they'll be created on first use/onboarding instead). The reverse
removes them only if no CustomField references them.
"""
from django.conf import settings
from django.db import migrations

SELECT_TYPES = ["select", "multiselect"]


def seed(apps, schema_editor):
    FieldType = apps.get_model("recaps", "CustomRecapFieldType")
    User = apps.get_model(settings.AUTH_USER_MODEL)
    creator = (
        User.objects.filter(is_superuser=True).order_by("id").first()
        or User.objects.order_by("id").first()
    )
    if creator is None:
        return
    for name in SELECT_TYPES:
        FieldType.objects.get_or_create(name=name, defaults={"created_by": creator})


def unseed(apps, schema_editor):
    FieldType = apps.get_model("recaps", "CustomRecapFieldType")
    CustomField = apps.get_model("recaps", "CustomField")
    for name in SELECT_TYPES:
        ft = FieldType.objects.filter(name=name).first()
        if ft and not CustomField.objects.filter(
            custom_field_type_id=ft.id
        ).exists():
            ft.delete()


class Migration(migrations.Migration):
    dependencies = [("recaps", "0026_customfield_options")]
    operations = [migrations.RunPython(seed, unseed)]
