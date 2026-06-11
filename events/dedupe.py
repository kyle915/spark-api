"""Duplicate-event detection + merge.

Bulk uploads and ad-hoc logging produced clusters of same-name events on
the same date ("five identical Albertsons"), which double-count on the
tracker and dashboards and make every event picker ambiguous.

``find_duplicate_clusters`` groups a tenant's events by normalized name
and chains events whose dates fall within ``day_window`` days into one
cluster (2+ events = reportable).

``merge_events`` folds duplicate events into a keeper inside one
transaction:

  * roster rows (``AmbassadorEvent``) whose ambassador already sits on
    the keeper are DELETED (keeping the keeper's row + its stamps);
    the rest repoint;
  * every other relation that targets Event (recaps, custom recaps,
    attendance, pings, ratings, jobs, open shifts, receipts, invoice
    lines, calendar rows — enumerated dynamically from
    ``Event._meta.related_objects`` so new FKs can't be silently
    dropped) repoints to the keeper;
  * the duplicate event is deleted; if its Request differs from the
    keeper's and ends up with NO remaining events, we try to delete
    that request too — otherwise the repair-missing-events cron would
    RE-CREATE an event for the now-eventless approved request and
    resurrect the duplicate. A request that refuses deletion (RESTRICT
    children) is left in place and reported as a warning instead.

Any IntegrityError mid-merge rolls the whole merge back.
"""

from __future__ import annotations

import re
from collections import defaultdict

from django.db import transaction


def _norm_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def find_duplicate_clusters(
    tenant_id: int, *, day_window: int = 1, max_clusters: int = 50
) -> list[dict]:
    """Same-normalized-name events whose dates chain within ``day_window``
    days. Returns newest-first clusters of plain dicts (id/uuid/name/
    date/address + recap/roster counts so the admin can pick a keeper).
    """
    from django.db.models import Count

    from events.models import Event

    rows = list(
        Event.objects.filter(tenant_id=tenant_id)
        .annotate(
            legacy_recaps_n=Count("recaps", distinct=True),
            custom_recaps_n=Count("custom_recap", distinct=True),
            roster_n=Count("ambassadors_events", distinct=True),
        )
        .values(
            "id",
            "uuid",
            "name",
            "date",
            "address",
            "request_id",
            "legacy_recaps_n",
            "custom_recaps_n",
            "roster_n",
        )
    )

    by_name: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = _norm_name(r["name"])
        if key:
            by_name[key].append(r)

    clusters: list[dict] = []
    for key, group in by_name.items():
        if len(group) < 2:
            continue
        # Chain by date proximity: sort (nulls together at the end) and
        # split whenever the gap to the previous event exceeds the window.
        dated = sorted(
            (r for r in group if r["date"] is not None),
            key=lambda r: r["date"],
        )
        undated = [r for r in group if r["date"] is None]

        chains: list[list[dict]] = []
        for r in dated:
            if (
                chains
                and (r["date"] - chains[-1][-1]["date"]).days <= day_window
            ):
                chains[-1].append(r)
            else:
                chains.append([r])
        if len(undated) >= 2:
            chains.append(undated)

        for chain in chains:
            if len(chain) < 2:
                continue
            clusters.append(
                {
                    "key": key,
                    "events": [
                        {
                            "id": r["id"],
                            "uuid": str(r["uuid"]),
                            "name": r["name"] or "",
                            "date": (
                                r["date"].date().isoformat()
                                if r["date"]
                                else None
                            ),
                            "address": r["address"] or "",
                            "recaps_filed": r["legacy_recaps_n"]
                            + r["custom_recaps_n"],
                            "roster_count": r["roster_n"],
                        }
                        for r in chain
                    ],
                }
            )

    clusters.sort(
        key=lambda c: max(e["date"] or "" for e in c["events"]), reverse=True
    )
    return clusters[:max_clusters]


def merge_events(
    *, tenant_id: int, keep_event_id: int, merge_event_ids: list[int]
) -> dict:
    """Fold ``merge_event_ids`` into ``keep_event_id``. All events must
    belong to ``tenant_id``. Returns a report dict; raises ValueError on
    bad input (unknown ids, cross-tenant, keeper in the merge list).
    """
    from ambassadors.models import AmbassadorEvent
    from events.models import Event

    merge_ids = [i for i in {int(i) for i in merge_event_ids} if i != int(keep_event_id)]
    if not merge_ids:
        raise ValueError("Nothing to merge.")

    moved: dict[str, int] = defaultdict(int)
    deleted_events = 0
    deleted_requests = 0
    warnings: list[str] = []

    with transaction.atomic():
        try:
            keeper = Event.objects.select_for_update().get(
                id=keep_event_id, tenant_id=tenant_id
            )
        except Event.DoesNotExist:
            raise ValueError("Keeper event not found in this brand.")

        dups = list(
            Event.objects.select_for_update().filter(
                id__in=merge_ids, tenant_id=tenant_id
            )
        )
        if len(dups) != len(merge_ids):
            raise ValueError(
                "One or more events to merge weren't found in this brand."
            )

        for dup in dups:
            # Roster: a BA on BOTH events keeps the keeper's row.
            keeper_amb_ids = set(
                AmbassadorEvent.objects.filter(event=keeper).values_list(
                    "ambassador_id", flat=True
                )
            )
            clash = AmbassadorEvent.objects.filter(
                event=dup, ambassador_id__in=keeper_amb_ids
            )
            n_clash = clash.count()
            if n_clash:
                moved["roster rows dropped (BA already on keeper)"] += n_clash
                clash.delete()
            n = AmbassadorEvent.objects.filter(event=dup).update(event=keeper)
            if n:
                moved["AmbassadorEvent"] += n

            # Everything else that points at Event, discovered dynamically
            # so a future FK can't be silently orphaned.
            for rel in Event._meta.related_objects:
                model = rel.related_model
                if model is AmbassadorEvent:
                    continue
                fname = rel.field.name
                n = model.objects.filter(**{fname: dup}).update(
                    **{fname: keeper}
                )
                if n:
                    moved[model.__name__] += n

            dup_request_id = dup.request_id
            dup.delete()
            deleted_events += 1

            # Orphaned request guard — without this the repair-missing-
            # events cron re-creates an event for the approved, now
            # event-less request and the duplicate comes back.
            if dup_request_id and dup_request_id != keeper.request_id:
                still_has_events = Event.objects.filter(
                    request_id=dup_request_id
                ).exists()
                if not still_has_events:
                    from events.models import Request

                    try:
                        with transaction.atomic():
                            Request.objects.filter(
                                id=dup_request_id
                            ).delete()
                        deleted_requests += 1
                    except Exception:  # noqa: BLE001 — RESTRICT children
                        warnings.append(
                            f"Request #{dup_request_id} kept (has protected "
                            "children) — delete it manually or the repair "
                            "cron may re-create its event."
                        )

    return {
        "moved": dict(moved),
        "deleted_events": deleted_events,
        "deleted_requests": deleted_requests,
        "warnings": warnings,
    }
