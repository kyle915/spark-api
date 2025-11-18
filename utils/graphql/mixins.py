import strawberry
from typing import Any, Union, Type, TypeVar
from graphql import GraphQLError
from asgiref.sync import sync_to_async
from strawberry import relay


from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db.models import Model

from tenants.models import Tenant
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.permissions import StrictIsAuthenticated

User = get_user_model()


class SparkGraphQLMixin:
    """Mixin for Spark GraphQL operations."""

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

        if is_spark_request and has_explicit_tenant:
            tenant = await self._get_tenant_without_membership(
                tenant_id=tenant_id,
                tenant_uuid=tenant_uuid,
            )
        else:
            tenant = await self.get_tenant(
                user,
                tenant_id=tenant_id,
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
        tenant_id: int | None = None,
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
        try:
            return await sync_to_async(user.get_tenant)(
                tenant_id,
                tenant_uuid,
            )
        except Exception as e:
            raise GraphQLError(
                "It looks like you are not a member of this tenant.")

    async def _get_tenant_without_membership(
        self,
        tenant_id: int | str | None = None,
        tenant_uuid: str | None = None,
    ) -> Tenant:
        """Fetch a tenant directly without checking user membership."""
        filters: dict[str, Any] = {}

        if tenant_id is not None:
            try:
                filters["id"] = int(tenant_id)
            except (TypeError, ValueError):
                raise GraphQLError("Invalid tenant ID.")
        elif tenant_uuid:
            filters["uuid"] = tenant_uuid
        else:
            raise GraphQLError("Tenant identifier is required.")

        try:
            return await sync_to_async(Tenant.objects.get)(**filters)
        except Tenant.DoesNotExist:
            raise GraphQLError("Tenant not found.")


class BaseMutationService(SparkGraphQLMixin):
    """Base class for mutation services."""

    input: SparkGraphQLInput | None = None
    info: strawberry.Info | None = None
    user: User | None = None
    tenant_id: int | None = None
    is_public: bool = False
    is_spark_schema: bool = False

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

    def set_input(self, input: SparkGraphQLInput) -> "BaseMutationService":
        """Set the input for the service."""
        self.input = input
        return self

    async def set_user_and_tenant(self, info: strawberry.Info) -> "BaseMutationService":
        """Set the user and tenant for the service."""
        self.info = info
        self.user = await self.get_user(info)
        self.is_spark_schema = self.is_spark_schema_request(
            info, user=self.user)
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
        if (
            not self.is_public
            and not self.is_spark_schema
            and self.user.role.is_spark_admin
            and tenant_id
        ):
            raise GraphQLError("Tenant ID should not be provided.")

    async def _resolve_tenant_without_membership(
        self, tenant_id: Union[str, int]
    ) -> int:
        """Resolve tenant ID for Spark schema requests without membership restrictions."""
        try:
            tenant_pk = int(tenant_id)
        except (TypeError, ValueError):
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
        is_update: bool = hasattr(
            self.input, "id") and self.input.id is not None
        if is_update:
            model = await sync_to_async(model_class.objects.get)(id=self.input.id)
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
        for key, value in params.items():
            setattr(model, key, value)

        # set the tenant id
        setattr(model, "tenant_id", self.tenant_id)
        await sync_to_async(model.save)()
        return model


TModel = TypeVar("TModel", bound=Model)
TResponse = TypeVar("TResponse")
TCreateInput = TypeVar("TCreateInput")
TUpdateInput = TypeVar("TUpdateInput")


class BaseMutationMixin:
    """
    Base mixin for GraphQL mutations that provides common error handling.
    """

    response_class: Type
    """The response class type for the mutation."""

    def build_mutation_response(
        self,
        *,
        success: bool,
        message: str,
        input_obj: SparkGraphQLInput | None = None,
        **extra_fields: Any,
    ) -> Any:
        """
        Build a mutation response (success or error).

        Args:
            success: Whether the operation was successful
            message: Response message
            input_obj: The input object (optional, for client_mutation_id propagation)
            **extra_fields: Additional fields to include in the response (e.g., model instance)

        Returns:
            An instance of response_class with the provided fields
        """
        from utils.utils import build_mutation_response as _build_mutation_response
        return _build_mutation_response(
            self.response_class,
            success=success,
            message=message,
            input_obj=input_obj,
            **extra_fields,
        )

    def _get_message(self, message: str | None = None) -> str:
        model_name: str = "Object"
        if hasattr(self, "model_field_name") and self.model_field_name != "":
            model_name = self.model_field_name.replace("_", " ").title()
        default_message: str = f"{model_name} saved successfully."

        if message:
            return message
        return default_message


class CRUDMutationsMixin(BaseMutationMixin):
    """
    Mixin that automatically generates create and update mutations.

    Usage:
        class StatusMutations(CRUDMutationsMixin):
            service_class = StatusMutationService
            create_input_class = inputs.CreateStatusInput
            update_input_class = inputs.UpdateStatusInput
            response_class = types.StatusDetailResponse
            model_field_name = "status"  # field name in response (status, event, etc.)
            create_message = "Status created successfully."
            update_message = "Status updated successfully."
    """

    service_class: Type[BaseMutationService]
    create_input_class: Type
    update_input_class: Type
    response_class: Type
    model_field_name: str
    create_message: str | None = None
    update_message: str | None = None

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create(self, info: strawberry.Info, input) -> Type[TResponse]:
        """Create mutation - auto-generated."""
        try:
            model_instance = await self.service_class.process_create_or_update(
                input=input, info=info
            )
            return self.build_mutation_response(
                success=True,
                message=self._get_message(self.create_message),
                input_obj=input,
                **{self.model_field_name: model_instance}
            )
        except GraphQLError as e:
            return self.build_mutation_response(
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update(self, info: strawberry.Info, input) -> Type[TResponse]:
        """Update mutation - auto-generated."""
        try:
            model_instance = await self.service_class.process_create_or_update(
                input=input, info=info
            )
            return self.build_mutation_response(
                success=True,
                message=self._get_message(self.update_message),
                input_obj=input,
                **{self.model_field_name: model_instance}
            )
        except GraphQLError as e:
            return self.build_mutation_response(
                success=False,
                message=str(e),
                input_obj=input,
            )
