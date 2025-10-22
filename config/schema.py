import strawberry
from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
from gqlauth.core.middlewares import JwtSchema
from events.schema import EventsQuery
from tenants.schema import Mutation as TenantMutation, Query as TenantQuery
from strawberry.extensions import SchemaExtension
from graphql import GraphQLError

Query = merge_types("Query", (EventsQuery, TenantQuery))
Mutation = merge_types("Mutation", (TenantMutation,))


class BlockIntrospectionForAnonymous(SchemaExtension):
    def on_request_start(self):
        request = self.execution_context.context["request"]
        user = getattr(request, "user", None)
        query_str = self.execution_context.query.strip().lower()
        if not (user and user.is_authenticated):
            if "__schema" in query_str or "__type" in query_str:
                raise GraphQLError("Introspection is disabled for unauthenticated users.")

schema = JwtSchema(
    query=Query,
    mutation=Mutation,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
    ],
)
