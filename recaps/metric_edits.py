"""Audit trail + post-save refresh for staff corrections to submitted recaps.

``updateCustomRecap`` / ``updateRecap`` snapshot the recap's editable values
before and after the write; every changed value becomes one
:class:`recaps.models.RecapMetricEdit` row. Derived numbers (consumers
sampled, units sold, conversion) are read live from the field values by
Insights, the Recaps list, Master Tracker, plan results, the weekly digest
and exports, so the refresh here only covers what is stored or cached: the
data-quality flags, the dashboard cache and Spark-rendered PDFs. Nothing in
this module sends email.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from graphql import GraphQLError
from uuid6 import uuid7

from ambassadors.models import Ambassador
from recaps import models
from recaps.mutation_parts.notify import _compute_recap_data_quality_flags
from recaps.mutation_parts.pdf_helpers import _delete_spark_generated_pdfs
from tenants.dashboard.services import DashboardQueriesService

logger = logging.getLogger(__name__)

Snapshot = dict[str, tuple[str, str | None]]

_NUMBER_FIELD_TYPES = {"number", "integer", "int", "numeric"}
_NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _ba_label(ambassador, external_ba_name) -> str | None:
    user = getattr(ambassador, "user", None) if ambassador is not None else None
    if user is not None:
        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        return name or user.email
    return _text(external_ba_name)


def snapshot_custom_recap(custom_recap: models.CustomRecap) -> Snapshot:
    """Editable values on a CustomRecap, keyed by a stable field key."""
    snap: Snapshot = {
        "name": ("Recap title", _text(custom_recap.name)),
        "total_engagements": (
            "Total engagements",
            _text(custom_recap.total_engagements),
        ),
        "used_corpo_card": (
            "Corporate card used",
            _yes_no(custom_recap.used_corpo_card),
        ),
        "filling_for_ambassador": (
            "Filling for BA",
            _yes_no(custom_recap.filling_for_ambassador),
        ),
    }
    ambassador = (
        Ambassador.objects.select_related("user")
        .filter(id=custom_recap.ambassador_id)
        .first()
        if custom_recap.ambassador_id
        else None
    )
    snap["ambassador"] = (
        "Brand Ambassador",
        _ba_label(ambassador, custom_recap.external_ba_name),
    )
    for cfv in models.CustomFieldValue.objects.filter(
        custom_recap=custom_recap
    ).select_related("custom_field"):
        snap[f"field:{cfv.custom_field_id}"] = (
            cfv.custom_field.name,
            _text(cfv.value),
        )
    sample_totals: dict[int, tuple[str, int]] = {}
    for sample in models.CustomRecapProductSample.objects.filter(
        custom_recap=custom_recap
    ).select_related("product"):
        label, qty = sample_totals.get(
            sample.product_id, (f"{sample.product.name} · units sampled", 0)
        )
        sample_totals[sample.product_id] = (label, qty + (sample.quantity or 0))
    for product_id, (label, qty) in sample_totals.items():
        snap[f"sample:{product_id}"] = (label, str(qty))
    return snap


_RECAP_SCALARS: tuple[tuple[str, str], ...] = (
    ("name", "Recap title"),
    ("total_engagements", "Total engagements"),
    ("products_sold", "Products sold"),
    ("total_cans_sold", "Total cans sold"),
    ("total_packs_sold", "Total packs sold"),
    ("total_earnings", "Total earnings"),
    ("account_spend_amount", "Account spend amount"),
    ("traffic_description", "Traffic description"),
    ("competitive_presence", "Competitive presence"),
)


def snapshot_recap(recap: models.Recap) -> Snapshot:
    """Editable metric values on a standard (non-template) Recap."""
    snap: Snapshot = {
        key: (label, _text(getattr(recap, key, None)))
        for key, label in _RECAP_SCALARS
    }
    sample_totals: dict[int, tuple[str, int]] = {}
    for sample in models.ProductSamples.objects.filter(recap=recap).select_related(
        "product"
    ):
        label, qty = sample_totals.get(
            sample.product_id, (f"{sample.product.name} · units sampled", 0)
        )
        sample_totals[sample.product_id] = (label, qty + (sample.quantity or 0))
    for product_id, (label, qty) in sample_totals.items():
        snap[f"sample:{product_id}"] = (label, str(qty))
    return snap


def diff_snapshots(
    before: Snapshot, after: Snapshot
) -> list[tuple[str, str, str | None, str | None]]:
    """(field_key, label, old, new) for every value that changed."""
    changes = []
    for key in list(before) + [k for k in after if k not in before]:
        old_label, old = before.get(key, ("", None))
        new_label, new = after.get(key, ("", None))
        if old != new:
            changes.append((key, new_label or old_label, old, new))
    return changes


def record_edits(
    *,
    changes: Iterable[tuple[str, str, str | None, str | None]],
    user,
    reason: str | None,
    custom_recap: models.CustomRecap | None = None,
    recap: models.Recap | None = None,
) -> list[models.RecapMetricEdit]:
    batch = uuid7()
    rows = [
        models.RecapMetricEdit(
            custom_recap=custom_recap,
            recap=recap,
            batch=batch,
            field_key=key[:255],
            field_label=(label or key)[:255],
            old_value=old,
            new_value=new,
            reason=(reason or "").strip(),
            edited_by=user,
        )
        for key, label, old, new in changes
    ]
    return models.RecapMetricEdit.objects.bulk_create(rows)


def validate_number_field(field_name: str, field_type_name: str, value) -> None:
    """Reject negative / non-numeric values typed into a number field."""
    if (field_type_name or "").strip().lower() not in _NUMBER_FIELD_TYPES:
        return
    text = _text(value)
    if text is None:
        return
    if not _NUMBER_RE.match(text.replace(",", "")):
        raise GraphQLError(
            f"{field_name}: enter a whole number or decimal that is 0 or more."
        )


def refresh_after_edit(recap: models.CustomRecap | models.Recap) -> None:
    """Refresh stored/cached values that a metric correction makes stale.

    Sync — call via sync_to_async after the edit transaction commits.
    Best-effort: a refresh failure never fails the save.
    """
    if isinstance(recap, models.CustomRecap):
        # Re-stamp the flag only — the submit-time alert email is not resent.
        _compute_recap_data_quality_flags(recap)
        tenant_id = recap.tenant_id
    else:
        tenant_id = recap.event.tenant_id if recap.event_id else None

    try:
        _delete_spark_generated_pdfs(recap)
    except Exception:
        logger.exception("stale recap PDF cleanup failed for recap %s", recap.pk)

    try:
        for scope in {tenant_id or 0, 0}:
            DashboardQueriesService.invalidate_cache_for_tenant(
                scope, ["recap_dashboard"]
            )
    except Exception:
        logger.exception("dashboard cache bump failed for recap %s", recap.pk)
