from strawberry.extensions import SchemaExtension
from graphql import GraphQLError
from typing import Any, Type, TypeVar, Union

from utils.graphql.inputs import SparkGraphQLInput


class BlockIntrospectionForAnonymous(SchemaExtension):
    allowed_mutations = [
        "SocialAuthGoogle",
        "SocialAuthApple",
        "CreateRequest",
        "UpdateRequest"
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
