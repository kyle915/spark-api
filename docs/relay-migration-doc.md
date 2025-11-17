# Relay Migration README

This document summarizes the core changes introduced while migrating Spark's GraphQL API to the Relay specification. Use it as a quick reference for the new building blocks, how they fit together, and what to keep in mind when adding new queries or mutations.

## Goals of the migration
- Deliver Relay-compliant results (edges, pageInfo, totalCount) across every query.
- Keep mutation payloads consistent with the `clientMutationId` contract.
- Centralize the authentication/tenant logic reused by queries and mutations.
- Provide reusable utilities for input serialization and connection building.

## Shared utilities (`utils/graphql`)
- `relay.py`: defines `CountableConnection`/`CountableEdge`, cursor helpers (`encode_cursor`/`decode_cursor`) and the `connection_from_queryset_sync|async` builders. `_validate_limits` guarantees that only one of `first`/`last` is accepted and that limits respect the configured defaults.
- `inputs.py`: `SparkGraphQLInput` adds a `client_mutation_id` field plus `to_dict()` to convert Strawberry inputs into model-ready dictionaries without repetitive boilerplate.
- `mixins.py`: `SparkGraphQLMixin` handles user resolution, tenant lookups and the special Spark-schema flow that allows administrative queries to pass `tenant_id`/`tenant_uuid` explicitly.
- `permissions.py`: `StrictIsAuthenticated` is a reusable Strawberry permission that wraps the authentication check.
- `types.py`: `SparkGraphQLErrorResponse` unifies the error shape for public mutations.

## Relay queries
- `events/queries.py` now uses `BaseEventQueriesService` which encapsulates tenant filtering, `q` search, ordering, and the call to `connection_from_queryset_async`.
- Every resource (events, types, statuses, requests, clients, distributors, retailers, products, etc.) exposes:
  - A `get_connection(...)` helper returning a `CountableConnection`.
  - A `get_record(...)` helper for single-item fetching that reuses `SparkGraphQLMixin.get_user_tenant`.
- Explicit `default_limit` and `max_limit` values exist per collection (catalogs use 50/100, others use 10/50). Public signatures leave `first`/`last` optional so clients can decide the pagination direction.
- `tenants/schema.py` reuses the same helpers to paginate tenants for both the Spark and Clients schemas.

## Relay mutations
- `events/mutations.py` introduces `BaseMutationService`, which:
  - Binds inputs (subclasses of `SparkGraphQLInput`) to the current user/tenant, runs validations, and persists models.
  - Normalizes success/error payloads via `build_mutation_response`, ensuring `client_mutation_id` echoes back to the caller.
- Mutation groups (events, types, statuses, locations, etc.) simply delegate to these services, removing duplicated code and keeping Relay compliance.
- `tenants/mutations.py` uses the same shared utilities (`SparkGraphQLInput`, `ensure_relay_mutation`) for registration and social auth flows.

## Schemas and permissions
- `events/schema.py` splits queries/mutations per audience (Ambassadors, Clients, Spark) but each class inherits the Relay-ready resolvers.
- `config/schema_spark.py` merges `EventQuerySpark` + `QuerySpark`, and the mutation types, and applies the common extensions (`DjangoOptimizerExtension`, `BlockIntrospectionForAnonymous`).
- Generated schemas (`schema_clients.graphql`, etc.) now expose the Relay `Connection`, `Edge`, and `PageInfo` types plus the new inputs carrying `clientMutationId`.

## Usage examples
```graphql
query TenantEvents($tenant: ID!, $after: String) {
  tenantEvents(tenantUuid: $tenant, first: 20, after: $after) {
    totalCount
    edges {
      cursor
      node {
        id
        name
        status { name }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
```

```graphql
mutation CreateEvent($input: CreateEventInput!) {
  createEvent(input: $input) {
    success
    message
    clientMutationId
    event { id name }
  }
}

# input payload
{
  "input": {
    "clientMutationId": "create-evt-1",
    "name": "Launch Party",
    "eventTypeId": "RXZlbnRUeXBlOjE=",
    "statusId": "RXZlbnRTdGF0dXM6MQ=="
  }
}
```

## Final considerations
- Always send **only one** of `first` or `last`. If both are omitted, the resolver falls back to `default_limit`.
- Reuse `SparkGraphQLMixin` when building new query services to avoid duplicating tenant/authentication logic.
- Have every new input inherit from `SparkGraphQLInput` and use `build_mutation_response` so `clientMutationId` does not get lost.
- Use `connection_from_queryset_async` (or the sync variant) for new collections and pick meaningful `default_limit`/`max_limit` pairs for the domain.
