"""Seed the global default contractor agreement on deploy (idempotent).

Cloud Run runs migrations on deploy, so this makes the apply-time
agreement live immediately without a manual management-command run. Safe
to re-run: a no-op if any active global (tenant=None) agreement already
exists. Editing the text later is done via the seed command with a new
--version (a NEW row), so this never clobbers a hand-edited agreement.
"""

from django.db import migrations


def seed(apps, schema_editor):
    ContractorAgreement = apps.get_model("jobs", "ContractorAgreement")
    if ContractorAgreement.objects.filter(
        tenant__isnull=True, is_active=True
    ).exists():
        return
    # Pull the canonical default text from the seed command so the two
    # never drift.
    from jobs.management.commands.seed_contractor_agreement import (
        _DEFAULT_BODY,
        _DEFAULT_VERSION,
    )

    ContractorAgreement.objects.create(
        version=_DEFAULT_VERSION,
        body=_DEFAULT_BODY,
        is_active=True,
        tenant=None,
    )


def unseed(apps, schema_editor):
    # Reverse is a no-op — we don't delete an agreement BAs may have
    # already accepted against (acceptances reference it).
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0024_jobapplication_agreement_accepted_at_and_more"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
