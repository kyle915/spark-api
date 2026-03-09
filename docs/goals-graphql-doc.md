### Goals: Models, Queries, Mutations and Dashboard Progress

This document explains how **Goals** work in the Spark API, with a focus on:

- The underlying **data model** and how goals are scoped.
- The available **GraphQL queries and mutations**.
- How **Event Dashboard** computes and displays **Goals Progress**, including how it interacts with the **RMM filter**.

---

### 1. Data model and scope

Goals are stored in the `Goal` model in `tenants.models`:

- **One row per user, per tenant, per year**:
  - `tenant` – FK to `Tenant`.
  - `user` – FK to `AUTH_USER_MODEL`.
  - `year` – integer (e.g. `2025`).
  - Unique constraint: `(tenant, user, year)`.
- **Target fields** (nullable so you can set only some goals):
  - `event_target_goal: int | None`
  - `consumer_sampling_goal: int | None`
  - `brand_awareness_goal: float | None`
  - `purchase_intent_goal: float | None`
  - `female_participation_goal: float | None`
  - `first_time_buyers_goal: int | None`
- **Current values are never stored**; they are computed dynamically from:
  - `events.Event` (filtered by `tenant_id`, `rmm_asigned_id` and date/quarter).
  - `recaps.ConsumerEngagements` (aggregated over those events’ recaps).

This means:

- **Event target goal** is the _target_ number of events for that user + tenant + year.
- **Current event count** is the _actual_ number of events where that user is the assigned RMM in the selected period.

---

### 2. Service layer: GoalsService

Located in `[tenants/dashboard/goals_service.py](tenants/dashboard/goals_service.py)`.

Key functions:

- **`get_goals(tenant_id, user_id, year) -> Goal | None`**

  - Returns the `Goal` row for the given `(tenant, user, year)` or `None`.

- **`get_or_create_goal(tenant_id, user_id, year) -> (Goal, created)`**

  - Ensures a `Goal` row exists, initialized with all target fields as `None`.

- **`extract_goal_updates(obj) -> dict[str, int | float]`**

  - Reads all known target fields from an object (e.g. `SetGoalsInput`) and returns only the non-`None` ones.

- **`upsert_goals(tenant_id, user_id, year, goal_updates: dict[str, int | float] | None) -> Goal`**

  - Creates or updates the `Goal` row, applying only the keys present in `goal_updates`.
  - Used by the `setGoals` mutation.

- **`get_current_values_for_user(tenant_id, user_id, start_date, end_date) -> dict[str, int | float]`**

  - Computes **current** values for a user in a given period:
    - Counts events where `Event.tenant_id == tenant_id` and `Event.rmm_asigned_id == user_id`, with date in range (using `date`, `start_time` or `request.date`).
    - Aggregates `ConsumerEngagements` over recaps for those events:
      - `current_events_count`
      - `current_consumer_sampling`
      - `current_brand_awareness` (clamped 0–100)
      - `current_purchase_intent` (clamped 0–100)
      - `current_first_time_buyers`
      - `current_female_participation` is currently `None` (no source yet).

- **`build_goals_progress(goal: Goal, current_values: dict) -> list[dict]`**
  - Converts a `Goal` + current values into a list of **progress items**:
    - Each item: `{ name, target, current, percentage_complete }`.
    - Only includes goal types with a positive target (>0).
    - Percentages are capped at 100.

---

### 3. GraphQL types and inputs

Defined in `[tenants/dashboard/types.py](tenants/dashboard/types.py)` and `[tenants/dashboard/inputs.py](tenants/dashboard/inputs.py)`.

- **Goal type**

  ```graphql
  type Goal {
    id: ID!
    uuid: String!
    tenantId: ID!
    userId: ID!
    year: Int!
    eventTargetGoal: Int
    consumerSamplingGoal: Int
    brandAwarenessGoal: Float
    purchaseIntentGoal: Float
    femaleParticipationGoal: Float
    firstTimeBuyersGoal: Int
    currentEventsCount: Int
    currentConsumerSampling: Int
    currentBrandAwareness: Float
    currentPurchaseIntent: Float
    currentFirstTimeBuyers: Int
    currentFemaleParticipation: Float
  }
  ```

- **Goals progress type (for Event Dashboard)**:

  ```graphql
  type GoalProgress {
    name: String!
    target: Float!
    current: Float!
    percentageComplete: Float! # 0–100
  }

  type EventDashboard {
    # existing fields...
    goalsProgress: [GoalProgress!]
  }
  ```

- **Input for setting goals**:

  ```graphql
  input SetGoalsInput {
    tenantId: ID!
    year: Int!
    eventTargetGoal: Int
    consumerSamplingGoal: Int
    brandAwarenessGoal: Float
    purchaseIntentGoal: Float
    femaleParticipationGoal: Float
    firstTimeBuyersGoal: Int
  }
  ```

  > Note: This input omits `userId` on purpose. Goals are always set **for the authenticated user**.

---

### 4. GraphQL queries

All queries live under `DashboardQueries` in `[tenants/dashboard/queries.py](tenants/dashboard/queries.py)` and are exposed in the **Client schema**.

#### 4.1 `goals` – fetch goals for a user

Signature (simplified):

```graphql
type DashboardQueries {
  goals(
    tenantId: ID!
    year: Int!
    userId: ID
    startDate: String
    endDate: String
    quarter: String
  ): Goal
}
```

Behavior:

- **Default mode (no `userId`)**:
  - Returns the **current user’s** goals for `(tenantId, year)`, if:
    - The user has an active `TenantedUser` for that tenant; otherwise returns `null`.
- **Specific user mode (`userId` provided)**:
  - `userId` is resolved to an integer via `resolve_id_to_int`.
  - The resolver still enforces:
    - The caller must be a tenanted user for `tenantId`.
    - Today, only **own goals** are returned: if `userId` does not match the current user, the resolver returns `null`.
    - This keeps behavior safe by default (no cross-user leakage). The structure is ready to be extended later if you decide to allow admins to see other users’ goals.
- **Current values**:
  - If `quarter` is provided, the resolver derives `(start_date, end_date)` using `DashboardQueriesService._parse_quarter`.
  - If `startDate`/`endDate` are provided, they are parsed as ISO dates.
  - When a valid date range is available, it calls `get_current_values_for_user` and populates the `current*` fields on the returned `Goal` type.

Example – **current user’s goals**:

```graphql
query MyGoals {
  goals(tenantId: "1", year: 2025) {
    year
    eventTargetGoal
    consumerSamplingGoal
    brandAwarenessGoal
    purchaseIntentGoal
  }
}
```

Example – **current user’s goals with current values**:

```graphql
query MyGoalsWithCurrent {
  goals(
    tenantId: "1"
    year: 2025
    startDate: "2025-01-01"
    endDate: "2025-12-31"
  ) {
    year
    eventTargetGoal
    currentEventsCount
    currentConsumerSampling
    currentBrandAwareness
    currentPurchaseIntent
  }
}
```

> If you pass an invalid `tenantId` or you are not a tenanted user for that tenant, `goals` returns `null`.

---

### 5. GraphQL mutations

All mutations live under `DashboardMutations` in `[tenants/dashboard/mutations.py](tenants/dashboard/mutations.py)`.

#### 5.1 `setGoals` – create or update goals for the authenticated user

Signature:

```graphql
type DashboardMutations {
  setGoals(input: SetGoalsInput!): Goal!
}
```

Behavior:

- Requires authentication (`StrictIsAuthenticated`).
- Resolves the **tenant** with `get_user_tenant(info, tenant_id=input.tenant_id, user=user)` to ensure the caller has access.
- Calls `upsert_goals(tenant.id, user.id, input.year, extract_goal_updates(input))`.
- Returns the updated `Goal` for that `(tenant, user, year)`.

Example:

```graphql
mutation SetMy2025Goals {
  setGoals(
    input: {
      tenantId: "1"
      year: 2025
      eventTargetGoal: 130
      consumerSamplingGoal: 12500
      brandAwarenessGoal: 80
      purchaseIntentGoal: 70
      firstTimeBuyersGoal: 200
    }
  ) {
    id
    year
    tenantId
    userId
    eventTargetGoal
    consumerSamplingGoal
    brandAwarenessGoal
    purchaseIntentGoal
    firstTimeBuyersGoal
  }
}
```

#### 5.2 `enqueueCreateGoalsForTenant`

Enqueues a background job that will ensure every **active user** in a tenant has a `Goal` row for a given year.

```graphql
mutation EnqueueCreateGoalsForTenant {
  enqueueCreateGoalsForTenant(tenantId: "1", year: 2025) {
    success
    enqueued
  }
}
```

Implementation:

- Mutation calls `Queues().default.add(create_goals_for_tenant, tenant.id, year)`.
- The task `create_goals_for_tenant` (in `tenants/dashboard/tasks.py`) calls `ensure_goals_for_tenant_users(tenant_id, year)` which:
  - Finds all active `TenantedUser` rows for the tenant.
  - Uses **bulk_create** to create missing `Goal` rows in batches for scalability (works for 10k+ users).

#### 5.3 `enqueueCreateGoalsForAllTenants`

Enqueues one job that, in turn, enqueues **one `create_goals_for_tenant` job per tenant**.

```graphql
mutation EnqueueCreateGoalsForAllTenants {
  enqueueCreateGoalsForAllTenants(year: 2025) {
    success
    enqueued
  }
}
```

Implementation:

- Mutation calls `Queues().default.add(create_goals_for_all_tenants, year)`.
- Task `create_goals_for_all_tenants`:
  - Iterates over tenant IDs using `Tenant.objects.values_list("id", flat=True).iterator(chunk_size=ENQUEUE_TENANTS_CHUNK_SIZE)`.
  - Enqueues `create_goals_for_tenant(tenant_id, year)` for each tenant.
  - Returns the number of enqueued jobs.

---

### 6. Event Dashboard and Goals Progress

The **Event Dashboard** query (`eventDashboard` in `DashboardQueries`) now includes a `goalsProgress` field whose behavior is aligned with the dashboard filters, especially the **RMM filter**.

#### 6.1 How goalsProgress is computed

Inside `eventDashboard`, the resolver calls:

```python
goals_progress = await _resolve_goals_progress(
    info, service, filters, start_date, end_date
)
```

`_resolve_goals_progress`:

1. Resolves the **tenant** (from filters or the user’s first active tenant).
2. Determines the **target year**:
   - Uses `filters.year` when provided.
   - Otherwise falls back to `start_date.year` from the dashboard date range.
3. Chooses a **target user**:
   - Default: the authenticated user (`info.context.request.user.id`).
   - If `filters.rmm_asigned_id` is provided:
     - Resolves it to an integer user id.
     - If the requester’s role is **client** or **spark-admin**, the target user is switched to that RMM id.
     - Otherwise, it continues to use the authenticated user (no cross-user leakage for ambassadors/other roles).
4. Fetches `Goal` and current values for `(tenant, target_user, year)`:
   - `get_goals(resolved_tenant_id, target_user_id, year)`
   - `get_current_values_for_user(resolved_tenant_id, target_user_id, start_date, end_date)`
5. Converts them into `GoalProgress` list using `build_goals_progress`.

If any of these steps fail (no tenant, no goal for that year, etc.), `goalsProgress` is `null`.

#### 6.2 Examples

- **Default dashboard (no RMM filter)** – current user’s goals:

  ```graphql
  query EventDashboardDefault {
    eventDashboard {
      metrics {
        totalEvents
        consumersSampled
      }
      goalsProgress {
        name
        target
        current
        percentageComplete
      }
    }
  }
  ```

  - `goalsProgress` reflects the **authenticated user’s** goals and current values for the inferred year.

- **Dashboard filtered by RMM** – RMM user’s goals:

  ```graphql
  query EventDashboardByRmm($rmmId: ID!) {
    eventDashboard(filters: { rmmAsignedId: $rmmId, year: 2025 }) {
      metrics {
        totalEvents
        consumersSampled
      }
      goalsProgress {
        name
        target
        current
        percentageComplete
      }
    }
  }
  ```

  - If the caller is a **client** or **spark-admin** for the tenant:
    - `goalsProgress` will show progress for the **filtered RMM user**.
  - If the caller is not allowed to see others’ goals (e.g. ambassador only):
    - `goalsProgress` will fall back to the **caller’s own goals**.

This keeps the UX consistent with the discussion:

- Metrics follow the **RMM filter**.
- Goals Progress now also follows the **RMM filter**, when the caller has permissions, or defaults to the current user otherwise.

---

### 7. Summary

- Goals are **per user, per tenant, per year**, with dynamic current values derived from events and consumer engagements.
- The API exposes:
  - `goals(tenantId, year, userId?, startDate?, endDate?, quarter?)` to read a `Goal` and optionally its current values.
  - `setGoals(input: SetGoalsInput!)` to create/update goals for the authenticated user.
  - `enqueueCreateGoalsForTenant` and `enqueueCreateGoalsForAllTenants` to backfill goal rows in the background.
- The Event Dashboard’s `goalsProgress` now:
  - Aligns with the RMM filter for client/spark-admin users.
  - Safely defaults to the authenticated user when no filter is present or when the caller is not allowed to view other users’ goals.
