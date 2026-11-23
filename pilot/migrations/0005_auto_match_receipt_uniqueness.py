# Unique constraints for OCR receipt → product-spend auto-match.

from django.conf import settings
from django.db import migrations, models
from django.db.models import Count


def _dedupe_before_constraints(apps, schema_editor):
    BrandCostLine = apps.get_model("pilot", "BrandCostLine")
    CostEvidenceLink = apps.get_model("pilot", "CostEvidenceLink")

    # Keep the oldest auto-booked recap-spend line per (tenant, source_reference).
    dup_refs = (
        BrandCostLine.objects.filter(source_reference__startswith="recap-product-spend:")
        .values("tenant_id", "source_reference")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    for row in dup_refs:
        keep_id = (
            BrandCostLine.objects.filter(
                tenant_id=row["tenant_id"],
                source_reference=row["source_reference"],
            )
            .order_by("id")
            .values_list("id", flat=True)
            .first()
        )
        BrandCostLine.objects.filter(
            tenant_id=row["tenant_id"],
            source_reference=row["source_reference"],
        ).exclude(id=keep_id).delete()

    # Keep the oldest evidence link per receipt_reading.
    dup_readings = (
        CostEvidenceLink.objects.filter(receipt_reading_id__isnull=False)
        .values("receipt_reading_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    for row in dup_readings:
        keep_id = (
            CostEvidenceLink.objects.filter(
                receipt_reading_id=row["receipt_reading_id"]
            )
            .order_by("id")
            .values_list("id", flat=True)
            .first()
        )
        CostEvidenceLink.objects.filter(
            receipt_reading_id=row["receipt_reading_id"]
        ).exclude(id=keep_id).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0001_initial"),
        ("pilot", "0004_merge_onboarding_attribution_and_pos_feeds"),
        ("recaps", "0056_checkin_recap_submission"),
        ("tenants", "0068_checkin_timesheet_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(_dedupe_before_constraints, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="brandcostline",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("source_reference__startswith", "recap-product-spend:")
                ),
                fields=("tenant", "source_reference"),
                name="pilot_cost_recap_spend_src_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="costevidencelink",
            constraint=models.UniqueConstraint(
                condition=models.Q(("receipt_reading__isnull", False)),
                fields=("receipt_reading",),
                name="pilot_cost_evidence_reading_uniq",
            ),
        ),
    ]
