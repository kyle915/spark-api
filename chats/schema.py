"""Bundle chat queries + mutations into per-schema (spark / mobile) types.

The chat surface is identical for admin (spark schema) and ambassador
(mobile schema) callers — the resolver-level role gate in
`services.resolve_caller_context` shapes the result set per side, so we
don't need to fork resolvers. Keeping the bundling parallel to other
apps (jobs, recaps) for consistency.
"""
import strawberry

from chats import mutations, queries


@strawberry.type
class ChatQuerySpark(queries.ChatQueries):
    pass


@strawberry.type
class ChatQueryMobile(queries.ChatQueries):
    pass


@strawberry.type
class ChatMutationsSpark(mutations.ChatMutations):
    pass


@strawberry.type
class ChatMutationsMobile(mutations.ChatMutations):
    pass


# The admin web app queries the CLIENT surface (/graphql/clients), so chat
# must be exposed there too — same resolvers, role-shaped per caller by
# services.resolve_caller_context.
@strawberry.type
class ChatQueryClient(queries.ChatQueries):
    pass


@strawberry.type
class ChatMutationsClient(mutations.ChatMutations):
    pass
