from strawberry.extensions import SchemaExtension
from graphql import GraphQLError
from typing import Any, Type, TypeVar, Union

from utils.graphql.inputs import SparkGraphQLInput


class BlockIntrospectionForAnonymous(SchemaExtension):
    allowed_mutations = [
        "SocialAuthGoogle",
        "SocialAuthApple",
        "CreateRequest",
        "UpdateRequest",
        "CreatePublicAmbassador",
        "AcceptAmbassadorInvitation",
    ]

    def on_request_start(self):
        request = self.execution_context.context["request"]
        user = getattr(request, "user", None)
        query_str = (self.execution_context.query or "").strip().lower()

        if not (user and user.is_authenticated):
            if self.execution_context.operation_name in self.allowed_mutations:
                return True  # Allow the mutation to be executed

            if "__schema" in query_str or "__type" in query_str:
                raise GraphQLError(
                    "Introspection is disabled for unauthenticated users.")


class ROLE_ID:
    Ambassadors = 1
    SparkAdmin = 2


MutationResponseType = TypeVar("MutationResponseType")


def build_mutation_response(
    response_cls: Type[MutationResponseType],
    *,
    success: bool,
    message: str,
    input_obj: SparkGraphQLInput | None = None,
    **extra_fields: Any,
) -> MutationResponseType:
    """Helper to keep relay clientMutationId propagation consistent."""
    client_mutation_id = getattr(input_obj, "client_mutation_id", None)
    return response_cls(
        success=success,
        message=message,
        client_mutation_id=client_mutation_id,
        **extra_fields,
    )


def default_tenant_theme():
    """
    Default DaisyUI-compatible theme variables for a tenant.

    Stored as a flat mapping of CSS custom property names to string values so
    the frontend can apply them directly.
    """
    return {
        "color-scheme": "dark",
        "--color-base-100": "oklch(14% 0 0)",
        "--color-base-200": "oklch(20% 0 0)",
        "--color-base-300": "oklch(26% 0 0)",
        "--color-base-content": "oklch(97% 0 0)",
        "--color-primary": "oklch(88.7% 0.182 95.352)",
        "--color-primary-content": "oklch(24.3% 0.049 266.472)",
        "--color-secondary": "oklch(100% 0 0)",
        "--color-secondary-content": "oklch(76.7% 0.140 91.177)",
        "--color-accent": "hsl(358, 96%, 42%)",
        "--color-accent-content": "hsl(358, 93%, 94%)",
        "--color-neutral": "oklch(24.3% 0.049 266.472)",
        "--color-neutral-content": "oklch(76.7% 0.140 91.177)",
        "--color-info": "hsl(246, 60%, 42%)",
        "--color-info-content": "hsl(247, 61% , 80%)",
        "--color-success": "oklch(59% 0.145 163.225)",
        "--color-success-content": "oklch(97% 0.021 166.113)",
        "--color-warning": "hsl(35, 96% , 50%)",
        "--color-warning-content": "hsl(36, 96%, 20%)",
        "--color-error": "oklch(41.2% 0.154 9.509)",
        "--color-error-content": "hsl(358, 100%, 80%)",
        "--radius-selector": "0.25rem",
        "--radius-field": "1rem",
        "--radius-box": "1rem",
        "--size-selector": "0.25rem",
        "--size-field": "0.25rem",
        "--border": "1px",
        "--depth": "1",
        "--noise": "0",
    }
