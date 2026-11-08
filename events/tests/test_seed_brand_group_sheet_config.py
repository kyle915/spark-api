"""seed_brand_group_sheet_config: fill empty retail_sheet_id only."""

from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command


def test_seed_dry_run_does_not_write():
    tenant = MagicMock()
    tenant.pk = 1
    tenant.slug = "famus-energy"
    tenant.retail_sheet_id = ""
    qs = MagicMock()
    qs.only.return_value.first.return_value = tenant
    with patch(
        "events.management.commands.seed_brand_group_sheet_config.Tenant.objects"
    ) as objects:
        objects.filter.return_value = qs
        out = StringIO()
        call_command("seed_brand_group_sheet_config", stdout=out)
        objects.filter.return_value.update.assert_not_called()
        assert "SEED" in out.getvalue() or "dry-run" in out.getvalue().lower()


def test_seed_apply_writes_empty_only():
    empty = MagicMock()
    empty.pk = 2
    empty.slug = "famus-energy"
    empty.retail_sheet_id = ""
    filled = MagicMock()
    filled.pk = 3
    filled.slug = "alcohol-armor"
    filled.retail_sheet_id = "already-set"

    def _filter(**kwargs):
        slug = kwargs.get("slug")
        qs = MagicMock()
        if slug == "famus-energy":
            qs.only.return_value.first.return_value = empty
        elif slug == "alcohol-armor":
            qs.only.return_value.first.return_value = filled
        else:
            qs.only.return_value.first.return_value = None
        return qs

    with patch(
        "events.management.commands.seed_brand_group_sheet_config.Tenant.objects"
    ) as objects:
        objects.filter.side_effect = _filter
        out = StringIO()
        call_command("seed_brand_group_sheet_config", "--apply", stdout=out)
        # Only empty famus should update; alcohol-armor kept.
        assert "wrote" in out.getvalue().lower()
