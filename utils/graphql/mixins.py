import base64
import logging
import strawberry
from typing import Any, Union, Type
from graphql import GraphQLError
from asgiref.sync import sync_to_async


from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db.models import Model

from tenants.models import Tenant
from utils.graphql.inputs import SparkGraphQLInput

User = get_user_model()
logger = logging.getLogger(__name__)


def decode_global_id(global_id: str) -> int:
    """
    Decode a strawberry-relay globalId to extract the database ID.

    GlobalIds are base64 encoded strings in the format "TypeName:ID".
    This function decodes the base64 and extracts the numeric ID.

    Args:
        global_id: The globalId string (e.g., "VGVuYW50VHlwZTox")

    Returns:
        The numeric database ID

    Raises:
        GraphQLError: If the globalId cannot be decoded or is invalid
    """
    import base64

    try:
        # Decode base64
        decoded = base64.b64decode(global_id.encode("utf-8")).decode("utf-8")
        # Extract ID after the colon (format: "TypeName:ID")
        if ":" not in decoded:
            raise ValueError("Invalid globalId format")
        _, db_id = decoded.split(":", 1)
        return int(db_id)
    except (ValueError, TypeError, UnicodeDecodeError) as e:
        raise GraphQLError(f"Invalid globalId: {global_id}") from e


def resolve_id_to_int(id_value: str | int) -> int:
    """
    Resolve an ID value that could be either a globalId or a direct integer.

    Args:
        id_value: Either a globalId string or an integer/string integer

    Returns:
        The numeric database ID
    """
    if isinstance(id_value, int):
        return id_value

    if isinstance(id_value, str):
        # If it's a pure digit string, convert directly
        if id_value.isdigit():
            return int(id_value)
        # Otherwise, try to decode as globalId
        return decode_global_id(id_value)

    raise GraphQLError(f"Invalid ID format: {id_value}")


class SparkGraphQLMixin:
    """Mixin for Spark GraphQL operations."""

    @staticmethod
    def get_role_slug(user: User | None) -> str:
        """Return normalized role slug for a user.

        A user on IGNITE_ADMIN_EXCLUDE (a removed Ignite admin) is reported as
        "client" rather than their stored "spark-admin" role: this is the
        inverted-gate-safe value — every ``role == "spark-admin"`` admin check
        sees a non-admin, and the ``role == "client"`` gates restrict them to
        their own tenant (they have none) instead of falling through to the
        admin "see everything" branch. The authoritative resolver
        (resolve_request_user_access) already denies them on the query side;
        this keeps the direct-read mutation paths safe too. Reversible: drop the
        entry from IGNITE_ADMIN_EXCLUDE."""
        from utils.graphql.permissions import IGNITE_ADMIN_EXCLUDE
        if (getattr(user, "email", "") or "").lower() in IGNITE_ADMIN_EXCLUDE:
            return "client"
        role = getattr(user, "role", None)
        return (getattr(role, "slug", None) or "").lower()

    def is_spark_schema_request(
        self,
        info: strawberry.Info,
        user: User | None = None,
    ) -> bool:
        """Determine if the current request is made by a Spark admin user."""
        if not user:
            request = getattr(info.context, "request", None)
            user = getattr(request, "user", None) if request else None

        if not user or not getattr(user, "is_authenticated", False):
            return False

        role = getattr(user, "role", None)
        slug = getattr(role, "slug", None) if role else None
        return (slug or "").lower() == "spark-admin"

    async def get_user_tenant(
        self,
        info: strawberry.Info,
        tenant_id: int | str | None = None,
        tenant_uuid: str | None = None,
        user: User | None = None,
    ) -> Tenant:
        """Get the tenant for the user.

        Args:
            info (strawberry.Info): The GraphQL resolve info.
            tenant_id (int | None, optional): The tenant id. Defaults to None.
            tenant_uuid (str | None, optional): The tenant uuid. Defaults to None.
        Returns:
            Tenant: The tenant for the user.
        """
        user = user or await self.get_user(info)
        is_spark_request = self.is_spark_schema_request(info, user=user)
        has_explicit_tenant = tenant_id is not None or tenant_uuid is not None
        resolved_tenant_id: int | None = None
        if tenant_id is not None:
            try:
                resolved_tenant_id = resolve_id_to_int(tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")

        if is_spark_request and has_explicit_tenant:
            tenant = await self._get_tenant_without_membership(
                tenant_id=resolved_tenant_id,
                tenant_uuid=tenant_uuid,
            )
        else:
            tenant = await self.get_tenant(
                user,
                tenant_id=resolved_tenant_id,
                tenant_uuid=tenant_uuid,
            )

        return tenant

    async def get_user(self, info: strawberry.Info) -> User:
        """Get the user for the request.

        Args:
            info (strawberry.Info): The info object.

        Returns:
            User: The user for the request.
        """
        user = info.context.request.user
        if not user or not user.is_authenticated or isinstance(user, AnonymousUser):
            raise GraphQLError(
                "Authentication required. Please provide a valid Auth token."
            )
        return user

    async def get_tenant(
        self,
        user: User,
        tenant_id: int | str | None = None,
        tenant_uuid: str | None = None,
    ) -> Tenant:
        """Get the tenant for the user.

        Args:
            user (User): The authenticated user.
            tenant_id (int | None, optional): The tenant id. Defaults to None.
            tenant_uuid (str | None, optional): The tenant uuid. Defaults to None.

        Returns:
            Tenant: The tenant for the user.
        """
        resolved_tenant_id: int | None = None
        if tenant_id is not None:
            try:
                resolved_tenant_id = resolve_id_to_int(tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")

        try:
            return await sync_to_async(user.get_tenant)(
                resolved_tenant_id,
                tenant_uuid,
            )
        except Exception as e:
            raise GraphQLError("It looks like you are not a member of this tenant.")

    async def _get_tenant_without_membership(
        self,
        tenant_id: int | str | None = None,
        tenant_uuid: str | None = None,
    ) -> Tenant:
        """Fetch a tenant directly without checking user membership."""
        filters: dict[str, Any] = {}

        if tenant_id is not None:
            try:
                filters["id"] = resolve_id_to_int(tenant_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError("Invalid tenant ID.")
        elif tenant_uuid:
            filters["uuid"] = tenant_uuid
        else:
            raise GraphQLError("Tenant identifier is required.")

        try:
            return await sync_to_async(Tenant.objects.get)(**filters)
        except Tenant.DoesNotExist:
            raise GraphQLError("Tenant not found.")

    async def resolve_tenant_id(
        self,
        info: strawberry.Info,
        filters: SparkGraphQLInput | None = None,
    ) -> int | str | None:
        """Resolve tenant id for queries; restrict only for client role."""
        filters_tenant_id = getattr(filters, "tenant_id", None)
        user = await self.get_user(info)
        role_slug = self.get_role_slug(user)
        resolved_tenant_id: int | None = None

        if filters_tenant_id is not None:
            try:
                resolved_tenant_id = resolve_id_to_int(filters_tenant_id)
            except (TypeError, ValueError, GraphQLError) as exc:
                raise GraphQLError("Invalid tenant ID.") from exc

        if role_slug == "client":
            tenant = await self.get_user_tenant(
                info,
                tenant_id=resolved_tenant_id,
            )
            return tenant.id

        return resolved_tenant_id


class BaseMutationService(SparkGraphQLMixin):
    """Base class for mutation services."""

    input: SparkGraphQLInput | None = None
    info: strawberry.Info | None = None
    user: User | None = None
    tenant_id: int | None = None
    is_public: bool = False
    is_spark_schema: bool = False

    # Response configuration - can be overridden by subclasses
    response_class: Type | None = None
    model_field_name: str = "model"
    create_message: str | None = None
    update_message: str | None = None

    @classmethod
    def with_input(cls, input: SparkGraphQLInput) -> "BaseMutationService":
        """Create a new instance of the service with the input."""
        service = cls()
        service.set_input(input)
        return service

    @classmethod
    async def process_create_or_update(
        cls, input: SparkGraphQLInput, info: strawberry.Info
    ) -> Model:
        """Process the create or update operation."""
        service = cls.with_input(input)
        await service.set_user_and_tenant(info)
        return await service.save()

    @classmethod
    def _get_default_message(cls, model_field_name: str, action: str) -> str:
        """Generate a default success message based on model_field_name and action."""
        if not model_field_name:
            return f"{action.capitalize()}d successfully."

        model_name = model_field_name.replace("_", " ").title()

        if action == "create":
            return f"{model_name} created successfully."
        elif action == "update":
            return f"{model_name} updated successfully."
        else:
            return f"{model_name} {action}d successfully."

    @classmethod
    def _build_mutation_response(
        cls,
        *,
        response_class: Type,
        success: bool,
        message: str,
        input_obj: SparkGraphQLInput | None = None,
        **extra_fields: Any,
    ) -> Any:
        """Build a mutation response (success or error)."""
        from utils.utils import build_mutation_response as _build_mutation_response

        return _build_mutation_response(
            response_class,
            success=success,
            message=message,
            input_obj=input_obj,
            **extra_fields,
        )

    @classmethod
    async def create(
        cls,
        input: SparkGraphQLInput,
        info: strawberry.Info,
        *,
        response_class: Type | None = None,
        model_field_name: str | None = None,
        create_message: str | None = None,
    ) -> Any:
        """
        Create mutation handler.

        Args:
            input: The input for the mutation
            info: Strawberry GraphQL info
            response_class: Response class type (uses cls.response_class if not provided)
            model_field_name: Field name in response (uses cls.model_field_name if not provided)
            create_message: Success message (uses cls.create_message if not provided)

        Returns:
            Response object with success/message and model instance
        """
        response_cls = response_class or cls.response_class
        field_name = model_field_name or cls.model_field_name
        message = create_message or cls.create_message

        if not response_cls:
            raise ValueError(
                "response_class must be provided either as class attribute or parameter"
            )

        try:
            model_instance: Model = await cls.process_create_or_update(
                input=input, info=info
            )

            # Generate message if not provided
            if not message:
                message = cls._get_default_message(field_name, "create")

            return cls._build_mutation_response(
                response_class=response_cls,
                success=True,
                message=message,
                input_obj=input,
                **{field_name: model_instance},
            )
        except GraphQLError as e:
            return cls._build_mutation_response(
                response_class=response_cls,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @classmethod
    async def update(
        cls,
        input: SparkGraphQLInput,
        info: strawberry.Info,
        *,
        response_class: Type | None = None,
        model_field_name: str | None = None,
        update_message: str | None = None,
    ) -> Any:
        """
        Update mutation handler.

        Args:
            input: The input for the mutation
            info: Strawberry GraphQL info
            response_class: Response class type (uses cls.response_class if not provided)
            model_field_name: Field name in response (uses cls.model_field_name if not provided)
            update_message: Success message (uses cls.update_message if not provided)

        Returns:
            Response object with success/message and model instance
        """
        response_cls: Type | None = response_class or cls.response_class
        field_name: str | None = model_field_name or cls.model_field_name
        message: str | None = update_message or cls.update_message

        if not response_cls:
            raise ValueError(
                "response_class must be provided either as class attribute or parameter"
            )

        try:
            model_instance: Model = await cls.process_create_or_update(
                input=input, info=info
            )

            # Generate message if not provided
            if not message:
                message = cls._get_default_message(field_name, "update")

            return cls._build_mutation_response(
                response_class=response_cls,
                success=True,
                message=message,
                input_obj=input,
                **{field_name: model_instance},
            )
        except GraphQLError as e:
            return cls._build_mutation_response(
                response_class=response_cls,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @classmethod
    async def delete(
        cls,
        input: SparkGraphQLInput,
        info: strawberry.Info,
        *,
        response_class: Type | None = None,
        model_field_name: str | None = None,
        delete_message: str | None = None,
    ) -> Any:
        """
        Delete mutation handler.

        Args:
            input: The input for the mutation (must have an 'id' field)
            info: Strawberry GraphQL info
            response_class: Response class type (uses cls.response_class if not provided)
            model_field_name: Field name in response (uses cls.model_field_name if not provided)
            delete_message: Success message (uses cls.delete_message if not provided)

        Returns:
            Response object with success/message
        """
        response_cls: Type | None = response_class or cls.response_class
        field_name: str | None = model_field_name or cls.model_field_name
        message: str | None = delete_message or getattr(cls, "delete_message", None)

        if not response_cls:
            raise ValueError(
                "response_class must be provided either as class attribute or parameter"
            )

        try:
            service = cls.with_input(input)
            await service.set_user_and_tenant(info)

            # Get the model instance to delete
            model_class = service.get_model()
            model_id = getattr(input, "id", None)
            if not model_id:
                raise GraphQLError("ID is required for delete operation.")

            # Resolve the ID (handles both integer IDs and Relay global IDs)
            try:
                resolved_id = resolve_id_to_int(model_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {model_id}")

            try:
                model = await sync_to_async(model_class.objects.get)(id=resolved_id)
            except model_class.DoesNotExist:
                raise GraphQLError(f"{model_class.__name__} not found.")

            # Delete the model
            await sync_to_async(model.delete)()

            # Generate message if not provided
            if not message:
                message = cls._get_default_message(field_name, "delete")

            return cls._build_mutation_response(
                response_class=response_cls,
                success=True,
                message=message,
                input_obj=input,
            )
        except GraphQLError as e:
            return cls._build_mutation_response(
                response_class=response_cls,
                success=False,
                message=str(e),
                input_obj=input,
            )

    def set_input(self, input: SparkGraphQLInput) -> "BaseMutationService":
        """Set the input for the service."""
        self.input = input
        return self

    async def set_user_and_tenant(self, info: strawberry.Info) -> "BaseMutationService":
        """Set the user and tenant for the service."""
        self.info = info
        self.user = await self.get_user(info)
        self.is_spark_schema = self.is_spark_schema_request(info, user=self.user)
        tenant_id = getattr(self.input, "tenant_id", None)

        if self.is_spark_schema and tenant_id:
            self.tenant_id = await self._resolve_tenant_without_membership(tenant_id)
        else:
            tenant = await self.get_tenant(self.user, tenant_id)
            self.tenant_id = tenant.id
        return self

    def set_is_public(self, is_public: bool) -> "BaseMutationService":
        """Set the is public for the service."""
        self.is_public = is_public
        return self

    def get_model(self) -> Model:
        """Get the model for the service."""
        raise NotImplementedError("Subclasses must implement this method.")

    async def validations(self):
        """Before save validations."""
        tenant_id = getattr(self.input, "tenant_id", None)
        if self.is_public and not tenant_id:
            raise GraphQLError("Tenant ID is required.")
        from utils.graphql.permissions import IGNITE_ADMIN_EXCLUDE
        _excluded = (
            getattr(self.user, "email", "") or ""
        ).lower() in IGNITE_ADMIN_EXCLUDE
        is_spark_admin = (
            await self.user.role.is_spark_admin
            if self.user and self.user.role and not _excluded
            else False
        )
        if (
            not self.is_public
            and not self.is_spark_schema
            and is_spark_admin
            and tenant_id
        ):
            raise GraphQLError("Tenant ID should not be provided.")

    async def _resolve_tenant_without_membership(
        self, tenant_id: Union[str, int]
    ) -> int:
        """Resolve tenant ID for Spark schema requests without membership restrictions."""
        try:
            tenant_pk = resolve_id_to_int(tenant_id)
        except (TypeError, ValueError, GraphQLError):
            raise GraphQLError("Invalid tenant ID.")

        try:
            await sync_to_async(Tenant.objects.get)(id=tenant_pk)
        except Tenant.DoesNotExist:
            raise GraphQLError("Tenant not found.")

        return tenant_pk

    async def save(self) -> Model:
        """Save the model."""
        # validate the input
        await self.validations()

        # get the model
        model_class = self.get_model()
        is_update: bool = hasattr(self.input, "id") and self.input.id is not None
        if is_update:
            model_id = getattr(self.input, "id", None)
            try:
                resolved_id = resolve_id_to_int(model_id)
            except (TypeError, ValueError, GraphQLError):
                raise GraphQLError(f"Invalid ID: {model_id}")
            model = await sync_to_async(model_class.objects.get)(id=resolved_id)
            if self.user:
                setattr(model, "updated_by", self.user)
        else:
            model = model_class()
            if self.user:
                setattr(model, "created_by", self.user)
            if self.is_public and self.input.tenant_id:
                self.tenant_id = self.input.tenant_id

        # set the parameters
        params: dict[str, Any] = self.input.to_dict(["tenant_id", "id"])
        for key, value in list(params.items()):
            if key.endswith("_id") and value is not None:
                try:
                    params[key] = resolve_id_to_int(value)
                except (TypeError, ValueError, GraphQLError):
                    raise GraphQLError(f"Invalid {key}: {value}")
        for key, value in params.items():
            setattr(model, key, value)

        # set the tenant id
        setattr(model, "tenant_id", self.tenant_id)
        await sync_to_async(model.save)()
        return model
