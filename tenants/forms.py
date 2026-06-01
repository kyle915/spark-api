"""Form Builder definitions — tenant-scoped GraphQL query + mutations.

Server-side home for the form DEFINITIONS the web Form Builder
(``SparkFormBuilder.tsx``) used to keep only in ``localStorage`` (under
``@spark.formBuilder/<tenantId>``), so they now persist per tenant and sync
across devices/teammates instead of being lost on a cache-clear. Backed by
:class:`tenants.models.CustomForm` (one row per built form; the whole builder
field-definition blob lives in a single free-form ``schema`` JSON column).

Exposed on the clients schema via two mixins merged into
``QueryClients`` / ``MutationClients`` in :mod:`tenants.schema`:

* ``tenantForms(tenantId)`` — list a tenant's saved form definitions.
* ``tenantForm(id)``        — fetch one definition by id.
* ``saveForm``              — create a new definition.
* ``updateForm``            — update an existing definition.
* ``deleteForm``            — delete a definition.

Every operation is TENANT-SCOPED using the same posture the rest of the
clients schema uses (see ``recaps/report_types.py`` ``resolve_target_tenant_id``
and ``utils/graphql/mixins.SparkGraphQLMixin``):

* **client role** — pinned to their OWN tenant; any ``tenantId`` argument is
  ignored/overridden so a client can never read or mutate another brand's
  forms.
* **admins** (spark-admin / staff / superuser / ``@igniteproductions.co``) —
  may target ANY tenant via ``tenantId``.

Every op carries ``StrictIsAuthenticated`` (so an unauthenticated request is
rejected up front, exactly like the rest of the tenant-scoped clients schema —
e.g. the recaps campaign-report queries). Past that gate the resolvers NEVER
raise on a bad/out-of-scope request: reads return ``[]`` / ``null`` for a form
the caller can't reach or on a DB hiccup, and writes return a safe
``success=False`` response. This keeps the Form Builder robust the same way the
Settings page (``preferences.py``) is.

This module is DEFINITION persistence only — collecting SUBMISSIONS (responses
to a published form) is a separate, future concern and is intentionally not
modeled here.
"""

import strawberry
from strawberry import relay
from strawberry.scalars import JSON
from asgiref.sync import sync_to_async

from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.mixins import SparkGraphQLMixin, resolve_id_to_int
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)
from .models import CustomForm


@strawberry.type
class CustomFormType:
    """A persisted Form Builder definition.

    ``schema`` is the builder's field-definition blob returned verbatim
    (description, internal/external flags, ordered ``fields``), so a saved
    form round-trips through the client unchanged.
    """

    id: strawberry.ID
    name: str
    schema: JSON
    is_published: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_model(cls, form: CustomForm) -> "CustomFormType":
        return cls(
            id=strawberry.ID(str(form.id)),
            name=form.name,
            schema=form.schema if isinstance(form.schema, dict) else {},
            is_published=bool(form.is_published),
            created_at=form.created_at.isoformat() if form.created_at else "",
            updated_at=form.updated_at.isoformat() if form.updated_at else "",
        )


@strawberry.type
class SaveFormResponse:
    success: bool
    message: str
    form: CustomFormType | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.type
class DeleteFormResponse:
    success: bool
    message: str
    deleted_id: strawberry.ID | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class SaveFormInput(SparkGraphQLInput):
    """Create a new Form Builder definition.

    ``tenant_id`` is honored only for admins (clients are pinned to their own
    tenant). ``schema`` is the builder's field-definition blob, stored
    verbatim.
    """

    name: str
    tenant_id: strawberry.ID | None = None
    schema: JSON | None = None
    is_published: bool | None = None


@strawberry.input
class UpdateFormInput(SparkGraphQLInput):
    """Update an existing Form Builder definition (by ``id``).

    Only the supplied fields change; omitted fields are left as stored. The
    target form must belong to a tenant the caller can access, or the update
    safely no-ops with ``success=False``.
    """

    id: strawberry.ID
    name: str | None = None
    schema: JSON | None = None
    is_published: bool | None = None


@strawberry.input
class DeleteFormInput(SparkGraphQLInput):
    """Delete a Form Builder definition (by ``id``)."""

    id: strawberry.ID


class _FormScope(SparkGraphQLMixin):
    """Tenant-scoping shell for Form Builder ops.

    Mirrors ``recaps/report_types.py`` ``_CampaignReportService``: clients are
    pinned to their own tenant, admins may target any tenant via an explicit
    id. Lives off the resolvers so each can resolve the concrete tenant the
    same way without duplicating the role logic.
    """

    async def resolve_target_tenant_id(
        self, info: strawberry.Info, requested_tenant_id
    ) -> int | None:
        """The CONCRETE tenant id the caller may operate on, or None.

        * **client** — always their own tenant; ``requested_tenant_id`` is
          ignored so they can never reach another brand's forms.
        * **admin** — the requested tenant id (global id or int), or None
          when none/garbage was passed (caller turns that into a safe
          failure rather than raising).
        """
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )

        if not _is_admin_access(role_slug, is_staff, is_super, email):
            tenant = await self.get_user_tenant(info)
            return tenant.id

        if requested_tenant_id is None:
            return None
        raw = str(requested_tenant_id).strip()
        if not raw:
            return None
        try:
            return resolve_id_to_int(raw)
        except Exception:
            return None

    async def accessible_tenant_ids(self, info: strawberry.Info) -> set[int] | None:
        """Tenant ids the caller may touch, or None for "any" (admins).

        Used by single-form lookups (fetch/update/delete) to confirm a form's
        tenant is in scope without trusting a client-supplied tenant id.
        """
        user = await self.get_user(info)
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )
        if _is_admin_access(role_slug, is_staff, is_super, email):
            return None

        @sync_to_async
        def _ids() -> set[int]:
            return set(
                user.tenanted_users.filter(is_active=True).values_list(
                    "tenant_id", flat=True
                )
            )

        return await _ids()


@strawberry.type
class TenantFormsQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_forms(
        self,
        info: strawberry.Info,
        tenant_id: strawberry.ID,
    ) -> list[CustomFormType]:
        """List a tenant's saved Form Builder definitions (newest first).

        Tenant-scoped: clients see only their own tenant's forms (the
        ``tenantId`` argument is overridden to their tenant); admins see the
        requested tenant's forms. Never raises — returns ``[]`` for an
        unauthenticated/out-of-scope request or on error.
        """
        scope = _FormScope()
        try:
            resolved = await scope.resolve_target_tenant_id(info, tenant_id)
        except Exception:
            return []
        if resolved is None:
            return []

        @sync_to_async
        def _load() -> list[CustomFormType]:
            forms = CustomForm.objects.filter(tenant_id=resolved)
            return [CustomFormType.from_model(f) for f in forms]

        try:
            return await _load()
        except Exception:
            return []

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def tenant_form(
        self,
        info: strawberry.Info,
        id: strawberry.ID,
    ) -> CustomFormType | None:
        """Fetch one Form Builder definition by id.

        Tenant-scoped: returns ``null`` when the form doesn't exist or its
        tenant is outside the caller's scope. Never raises.
        """
        scope = _FormScope()
        try:
            allowed = await scope.accessible_tenant_ids(info)
            form_pk = resolve_id_to_int(id)
        except Exception:
            return None

        @sync_to_async
        def _load() -> CustomFormType | None:
            form = CustomForm.objects.filter(pk=form_pk).first()
            if form is None:
                return None
            if allowed is not None and form.tenant_id not in allowed:
                return None
            return CustomFormType.from_model(form)

        try:
            return await _load()
        except Exception:
            return None


@strawberry.type
class TenantFormsMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def save_form(
        self,
        info: strawberry.Info,
        input: SaveFormInput,
    ) -> SaveFormResponse:
        """Create a new Form Builder definition for a tenant.

        Tenant-scoped: clients always save to their own tenant (any
        ``tenantId`` is ignored); admins save to the requested tenant.
        ``schema`` is stored verbatim. ``created_by`` is the authenticated
        user. Never raises — returns ``success=False`` on an out-of-scope or
        failed write.
        """
        scope = _FormScope()
        try:
            user = await scope.get_user(info)
        except Exception:
            return SaveFormResponse(
                success=False,
                message="User not authenticated.",
                client_mutation_id=input.client_mutation_id,
            )

        name = (input.name or "").strip()
        if not name:
            return SaveFormResponse(
                success=False,
                message="Form name is required.",
                client_mutation_id=input.client_mutation_id,
            )

        incoming_schema = input.schema
        if incoming_schema is not None and not isinstance(incoming_schema, dict):
            return SaveFormResponse(
                success=False,
                message="schema must be an object.",
                client_mutation_id=input.client_mutation_id,
            )

        try:
            resolved = await scope.resolve_target_tenant_id(info, input.tenant_id)
        except Exception:
            resolved = None
        if resolved is None:
            return SaveFormResponse(
                success=False,
                message="A valid tenant is required to save a form.",
                client_mutation_id=input.client_mutation_id,
            )

        @sync_to_async
        def _create() -> CustomFormType:
            form = CustomForm.objects.create(
                tenant_id=resolved,
                name=name,
                schema=incoming_schema or {},
                is_published=bool(input.is_published),
                created_by=user,
            )
            return CustomFormType.from_model(form)

        try:
            created = await _create()
        except Exception as exc:
            return SaveFormResponse(
                success=False,
                message=f"Could not save form: {exc}",
                client_mutation_id=input.client_mutation_id,
            )

        return SaveFormResponse(
            success=True,
            message="Form saved.",
            form=created,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_form(
        self,
        info: strawberry.Info,
        input: UpdateFormInput,
    ) -> SaveFormResponse:
        """Update an existing Form Builder definition (partial).

        Tenant-scoped: the target form must belong to a tenant the caller can
        access, or the update safely fails with ``success=False`` (a client
        can never edit another brand's form). Only supplied fields change.
        Never raises.
        """
        scope = _FormScope()
        try:
            allowed = await scope.accessible_tenant_ids(info)
            form_pk = resolve_id_to_int(input.id)
        except Exception:
            return SaveFormResponse(
                success=False,
                message="User not authenticated.",
                client_mutation_id=input.client_mutation_id,
            )

        incoming_schema = input.schema
        if incoming_schema is not None and not isinstance(incoming_schema, dict):
            return SaveFormResponse(
                success=False,
                message="schema must be an object.",
                client_mutation_id=input.client_mutation_id,
            )

        @sync_to_async
        def _update() -> CustomFormType | None:
            form = CustomForm.objects.filter(pk=form_pk).first()
            if form is None:
                return None
            if allowed is not None and form.tenant_id not in allowed:
                return None

            update_fields: list[str] = []
            if input.name is not None:
                name = input.name.strip()
                if name:
                    form.name = name
                    update_fields.append("name")
            if incoming_schema is not None:
                form.schema = incoming_schema
                update_fields.append("schema")
            if input.is_published is not None:
                form.is_published = bool(input.is_published)
                update_fields.append("is_published")

            if update_fields:
                update_fields.append("updated_at")
                form.save(update_fields=update_fields)
            return CustomFormType.from_model(form)

        try:
            updated = await _update()
        except Exception as exc:
            return SaveFormResponse(
                success=False,
                message=f"Could not update form: {exc}",
                client_mutation_id=input.client_mutation_id,
            )

        if updated is None:
            return SaveFormResponse(
                success=False,
                message="Form not found.",
                client_mutation_id=input.client_mutation_id,
            )

        return SaveFormResponse(
            success=True,
            message="Form updated.",
            form=updated,
            client_mutation_id=input.client_mutation_id,
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_form(
        self,
        info: strawberry.Info,
        input: DeleteFormInput,
    ) -> DeleteFormResponse:
        """Delete a Form Builder definition by id.

        Tenant-scoped: the target form must belong to a tenant the caller can
        access, or the delete safely fails with ``success=False``. Never
        raises.
        """
        scope = _FormScope()
        try:
            allowed = await scope.accessible_tenant_ids(info)
            form_pk = resolve_id_to_int(input.id)
        except Exception:
            return DeleteFormResponse(
                success=False,
                message="User not authenticated.",
                client_mutation_id=input.client_mutation_id,
            )

        @sync_to_async
        def _delete() -> bool | None:
            form = CustomForm.objects.filter(pk=form_pk).first()
            if form is None:
                return None
            if allowed is not None and form.tenant_id not in allowed:
                return None
            form.delete()
            return True

        try:
            deleted = await _delete()
        except Exception as exc:
            return DeleteFormResponse(
                success=False,
                message=f"Could not delete form: {exc}",
                client_mutation_id=input.client_mutation_id,
            )

        if not deleted:
            return DeleteFormResponse(
                success=False,
                message="Form not found.",
                client_mutation_id=input.client_mutation_id,
            )

        return DeleteFormResponse(
            success=True,
            message="Form deleted.",
            deleted_id=input.id,
            client_mutation_id=input.client_mutation_id,
        )
