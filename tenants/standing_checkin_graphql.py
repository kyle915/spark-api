"""GraphQL surface for Standing Check-In Studio (admin /walkups).

Staff-only ``updateStandingCheckinConfig`` edits buckets, BA resources,
location mode, and programs. The standing code is view-only here — mint
via ``issueCheckinCode``; never rotate a live code.
"""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async
from strawberry.scalars import JSON

from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)

logger = logging.getLogger(__name__)


@strawberry.input
class CheckinResourceInput:
    label: str
    url: str
    kind: str = "pdf"
    note: str | None = None
    hide_on_recap_only: bool | None = None


@strawberry.input
class UpdateStandingCheckinConfigInput(SparkGraphQLInput):
    tenant_id: strawberry.ID
    location_mode: str | None = None
    # Flat list or program-keyed map — same shape as Tenant.checkin_photo_buckets.
    photo_buckets: JSON | None = strawberry.UNSET
    resources: list[CheckinResourceInput] | None = None
    clear_resources: bool = False
    # First id = pinned default; full replace of selectable programs.
    event_type_ids: list[strawberry.ID] | None = None


@strawberry.type
class StandingCheckinResourceType:
    label: str
    kind: str
    url: str
    note: str | None = None
    hide_on_recap_only: bool = False


@strawberry.type
class StandingCheckinProgramType:
    id: strawberry.ID
    name: str
    is_default: bool


@strawberry.type
class UpdateStandingCheckinConfigResponse:
    success: bool
    message: str
    checkin_code: str | None = None
    location_mode: str | None = None
    photo_buckets: JSON | None = None
    resources: list[StandingCheckinResourceType] | None = None
    programs: list[StandingCheckinProgramType] | None = None
    changed_fields: list[str] | None = None
    client_mutation_id: strawberry.ID | None = None


def _resource_types(raw) -> list[StandingCheckinResourceType]:
    from tenants.models import normalize_checkin_resources

    out: list[StandingCheckinResourceType] = []
    for row in normalize_checkin_resources(raw):
        out.append(
            StandingCheckinResourceType(
                label=row["label"],
                kind=row["kind"],
                url=row["url"],
                note=row.get("note"),
                hide_on_recap_only=bool(row.get("hideOnRecapOnly")),
            )
        )
    return out


def _program_types(tenant) -> list[StandingCheckinProgramType]:
    default_id = getattr(tenant, "checkin_event_type_id", None)
    rows = list(tenant.checkin_event_types.all())
    rows.sort(key=lambda row: (0 if row.id == default_id else 1, (row.name or "").lower()))
    if not rows and getattr(tenant, "checkin_event_type", None) is not None:
        rows = [tenant.checkin_event_type]
    out: list[StandingCheckinProgramType] = []
    seen: set[int] = set()
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        out.append(
            StandingCheckinProgramType(
                id=strawberry.ID(str(row.id)),
                name=row.name or "",
                is_default=row.id == default_id,
            )
        )
    return out


def _snapshot(tenant) -> dict:
    from tenants.models import Tenant

    return {
        "checkin_code": (tenant.checkin_code or "").strip() or None,
        "location_mode": tenant.checkin_location_mode
        or Tenant.CHECKIN_LOCATION_ADDRESS,
        "photo_buckets": getattr(tenant, "checkin_photo_buckets", None),
        "resources": _resource_types(getattr(tenant, "checkin_resources", None)),
        "programs": _program_types(tenant),
    }


@strawberry.type
class StandingCheckinMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_standing_checkin_config(
        self,
        info: strawberry.Info,
        input: UpdateStandingCheckinConfigInput,
    ) -> UpdateStandingCheckinConfigResponse:
        """Edit standing check-in programs, photo buckets, BA PDFs, and
        location mode. Ignite staff only. Never remints or changes a live
        ``checkin_code`` — use ``issueCheckinCode`` to mint when empty."""

        def _fail(message: str) -> UpdateStandingCheckinConfigResponse:
            return UpdateStandingCheckinConfigResponse(
                success=False,
                message=message,
                client_mutation_id=input.client_mutation_id,
            )

        try:
            user = info.context.request.user
            role_slug, is_staff, is_super, email = await resolve_request_user_access(
                user
            )
        except Exception:  # noqa: BLE001
            return _fail("Sign in to edit the standing check-in.")
        if not _is_admin_access(role_slug, is_staff, is_super, email):
            return _fail("Only Ignite staff can edit the standing check-in.")

        try:
            tenant_pk = resolve_id_to_int(input.tenant_id)
        except Exception:  # noqa: BLE001
            return _fail("Brand not found.")

        photo_provided = input.photo_buckets is not strawberry.UNSET
        photo_value = None if not photo_provided else input.photo_buckets

        resource_dicts: list[dict] | None = None
        if input.clear_resources:
            resource_dicts = None
        elif input.resources is not None:
            resource_dicts = []
            for row in input.resources:
                item = {
                    "label": row.label,
                    "url": row.url,
                    "kind": row.kind,
                }
                if row.note:
                    item["note"] = row.note
                if row.hide_on_recap_only is True:
                    item["hideOnRecapOnly"] = True
                resource_dicts.append(item)

        event_ids: list[int] | None = None
        if input.event_type_ids is not None:
            event_ids = []
            for raw in input.event_type_ids:
                try:
                    event_ids.append(resolve_id_to_int(raw))
                except Exception:  # noqa: BLE001
                    return _fail(f"Invalid program id: {raw}")

        def _write():
            from tenants.models import Tenant
            from tenants.standing_checkin import update_standing_checkin_config

            tenant = Tenant.objects.filter(id=tenant_pk).first()
            if tenant is None:
                return None
            return update_standing_checkin_config(
                tenant,
                location_mode=input.location_mode,
                photo_buckets=photo_value,
                photo_buckets_provided=photo_provided,
                resources=resource_dicts,
                clear_resources=bool(input.clear_resources),
                event_type_ids=event_ids,
            )

        try:
            result = await sync_to_async(_write, thread_sensitive=True)()
        except ValueError as exc:
            return _fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "updateStandingCheckinConfig failed tenant=%s: %s", tenant_pk, exc
            )
            return _fail("Could not save standing check-in settings. Try again.")

        if result is None:
            return _fail("Brand not found.")

        tenant = result["tenant"]
        snap = _snapshot(tenant)
        changed = result.get("changed") or []
        if changed:
            message = "Standing check-in updated."
        else:
            message = "Already set — nothing to change."
        return UpdateStandingCheckinConfigResponse(
            success=True,
            message=message,
            checkin_code=snap["checkin_code"],
            location_mode=snap["location_mode"],
            photo_buckets=snap["photo_buckets"],
            resources=snap["resources"],
            programs=snap["programs"],
            changed_fields=list(changed),
            client_mutation_id=input.client_mutation_id,
        )
