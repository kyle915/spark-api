from strawberry.extensions import SchemaExtension
from graphql import GraphQLError


class BlockIntrospectionForAnonymous(SchemaExtension):
    allowed_mutations = ["SocialAuthGoogle", "SocialAuthApple"]

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
