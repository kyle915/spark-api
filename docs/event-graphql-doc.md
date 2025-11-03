# Events GraphQL API Documentation

This document provides comprehensive documentation for the Events GraphQL API endpoints.

## Table of Contents

- [Base URLs](#base-urls)
- [Authentication](#authentication)
- [Queries](#queries)
- [Mutations](#mutations)
- [Types](#types)
- [Input Types](#input-types)
- [Response Types](#response-types)
- [Examples](#examples)

---

## Base URLs

The API provides three different GraphQL endpoints based on user roles:

- **Ambassadors**: `http://localhost:8000/api/v1/graphql/ambassadors`
- **Spark Admin**: `http://localhost:8000/api/v1/graphql/spark`
- **Clients**: `http://localhost:8000/api/v1/graphql/clients`

---

## Authentication

All endpoints require authentication. You must include a JWT token in the Authorization header:

```
Authorization: Bearer <your_jwt_token>
```

To obtain a token, use the `tokenAuth` mutation:

```graphql
mutation {
  tokenAuth(email: "user@example.com", password: "password") {
    token
    refreshToken
    user {
      id
      email
      firstName
    }
  }
}
```

---

## Queries

### 1. Get Event Types

Retrieve all event types for the authenticated user's tenant.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Query:**
```graphql
query {
  eventTypes {
    id
    uuid
    name
    tenantId
    createdAt
    updatedAt
  }
}
```

**Response:**
```json
{
  "data": {
    "eventTypes": [
      {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Conference",
        "tenantId": "1",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    ]
  }
}
```

---

### 2. Get Events (Ambassadors/Clients)

Retrieve a paginated list of events for the authenticated user's tenant.

**Available for**: Ambassadors, Clients

**GraphQL Query:**
```graphql
query {
  events(
    limit: 10
    offset: 0
    q: "conference"
  ) {
    id
    uuid
    name
    createdAt
    updatedAt
    tenantId
    eventType {
      id
      name
    }
    status {
      id
      name
    }
  }
}
```

**Parameters:**
- `limit` (int, optional): Maximum number of events to return. Default: 10
- `offset` (int, optional): Number of events to skip for pagination. Default: 0
- `q` (string, optional): Search query to filter events by name (case-insensitive)

**Response:**
```json
{
  "data": {
    "events": [
      {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Annual Conference 2025",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z",
        "tenantId": "1",
        "eventType": {
          "id": "1",
          "name": "Conference"
        },
        "status": {
          "id": "1",
          "name": "Active"
        }
      }
    ]
  }
}
```

---

### 3. Get Events (Spark Admin)

Retrieve a paginated list of events. Spark Admins can query across all tenants.

**Available for**: Spark Admin only

**GraphQL Query:**
```graphql
query {
  events(
    limit: 20
    offset: 0
    tenantId: "1"
    q: "conference"
  ) {
    id
    uuid
    name
    createdAt
    updatedAt
    tenantId
    eventType {
      id
      name
    }
    status {
      id
      name
    }
  }
}
```

**Parameters:**
- `limit` (int, optional): Maximum number of events to return. Default: 10
- `offset` (int, optional): Number of events to skip for pagination. Default: 0
- `tenantId` (ID, optional): Filter events by tenant ID (Spark Admin only)
- `q` (string, optional): Search query to filter events by name (case-insensitive)

---

### 4. Get Single Event (Ambassadors/Clients)

Retrieve a single event by ID. Only returns events belonging to the user's tenant.

**Available for**: Ambassadors, Clients

**GraphQL Query:**
```graphql
query {
  event(id: "1") {
    id
    uuid
    name
    createdAt
    updatedAt
    tenantId
    eventType {
      id
      uuid
      name
    }
    status {
      id
      uuid
      name
    }
  }
}
```

**Parameters:**
- `id` (ID, required): The ID of the event to retrieve

**Response:**
```json
{
  "data": {
    "event": {
      "id": "1",
      "uuid": "01234567-89ab-cdef-0123-456789abcdef",
      "name": "Annual Conference 2025",
      "createdAt": "2025-01-15T10:00:00Z",
      "updatedAt": "2025-01-15T10:00:00Z",
      "tenantId": "1",
      "eventType": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Conference"
      },
      "status": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Active"
      }
    }
  }
}
```

---

### 5. Get Single Event (Spark Admin)

Retrieve a single event by ID. Spark Admins can access events from any tenant.

**Available for**: Spark Admin only

**GraphQL Query:**
```graphql
query {
  event(id: "1") {
    id
    uuid
    name
    createdAt
    updatedAt
    tenantId
    eventType {
      id
      name
    }
    status {
      id
      name
    }
  }
}
```

---

## Mutations

### 1. Create Event

Create a new event.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createEvent(input: {
    name: "Annual Conference 2025"
    eventTypeId: "1"
    statusId: "1"
    tenantId: "1"
  }) {
    success
    message
    event {
      id
      uuid
      name
      createdAt
      updatedAt
      tenantId
      eventType {
        id
        name
      }
      status {
        id
        name
      }
    }
  }
}
```

**Input Fields:**
- `name` (string, required): Event name (max 50 characters)
- `eventTypeId` (ID, required): ID of the event type
- `statusId` (ID, required): ID of the event status
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this; others use their default tenant)

**Response:**
```json
{
  "data": {
    "createEvent": {
      "success": true,
      "message": "Event created successfully.",
      "event": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Annual Conference 2025",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z",
        "tenantId": "1",
        "eventType": {
          "id": "1",
          "name": "Conference"
        },
        "status": {
          "id": "1",
          "name": "Active"
        }
      }
    }
  }
}
```

**Error Response:**
```json
{
  "data": {
    "createEvent": {
      "success": false,
      "message": "Validation errors: Name is required., Event type is required.",
      "event": null
    }
  }
}
```

---

### 2. Update Event

Update an existing event.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateEvent(input: {
    id: "1"
    name: "Updated Conference Name"
    eventTypeId: "2"
    statusId: "2"
    tenantId: "1"
  }) {
    success
    message
    event {
      id
      uuid
      name
      updatedAt
      eventType {
        id
        name
      }
      status {
        id
        name
      }
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the event to update
- `name` (string, required): Updated event name
- `eventTypeId` (ID, required): Updated event type ID
- `statusId` (ID, required): Updated status ID
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 3. Create Event Type

Create a new event type.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createEventType(input: {
    name: "Workshop"
    tenantId: "1"
  }) {
    success
    message
    eventType {
      id
      uuid
      name
      tenantId
      createdAt
      updatedAt
    }
  }
}
```

**Input Fields:**
- `name` (string, required): Event type name (max 50 characters)
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

**Response:**
```json
{
  "data": {
    "createEventType": {
      "success": true,
      "message": "Event type created successfully.",
      "eventType": {
        "id": "2",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Workshop",
        "tenantId": "1",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### 4. Update Event Type

Update an existing event type.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateEventType(input: {
    id: "1"
    name: "Updated Event Type Name"
    tenantId: "1"
  }) {
    success
    message
    eventType {
      id
      uuid
      name
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the event type to update
- `name` (string, required): Updated event type name
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 5. Create Event Status

Create a new event status.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createEventStatus(input: {
    name: "Cancelled"
    tenantId: "1"
  }) {
    success
    message
    eventStatus {
      id
      uuid
      name
      tenantId
      createdAt
      updatedAt
    }
  }
}
```

**Input Fields:**
- `name` (string, required): Event status name (max 50 characters)
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

**Response:**
```json
{
  "data": {
    "createEventStatus": {
      "success": true,
      "message": "Event status created successfully.",
      "eventStatus": {
        "id": "3",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Cancelled",
        "tenantId": "1",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### 6. Update Event Status

Update an existing event status.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateEventStatus(input: {
    id: "1"
    name: "Updated Status Name"
    tenantId: "1"
  }) {
    success
    message
    eventStatus {
      id
      uuid
      name
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the event status to update
- `name` (string, required): Updated event status name
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

## Types

### Event

Represents an event in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Event name
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)
- `tenantId` (ID): Tenant ID
- `eventType` (EventType, nullable): Associated event type
- `status` (EventStatus, nullable): Current event status

---

### EventType

Represents an event type.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Event type name
- `tenantId` (ID): Tenant ID
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### EventStatus

Represents an event status.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Status name
- `tenantId` (ID): Tenant ID
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

## Input Types

### CreateEventInput

**Fields:**
- `name` (string, required): Event name (max 50 characters)
- `eventTypeId` (ID, required): Event type ID
- `statusId` (ID, required): Event status ID
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty
- Event type ID is required
- Status ID is required
- Tenant ID validation depends on user role

---

### UpdateEventInput

Extends `CreateEventInput` with:
- `id` (ID, required): Event ID to update

---

### CreateEventTypeInput

**Fields:**
- `name` (string, required): Event type name (max 50 characters)
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty

---

### UpdateEventTypeInput

Extends `CreateEventTypeInput` with:
- `id` (ID, required): Event type ID to update

---

### CreateEventStatusInput

**Fields:**
- `name` (string, required): Event status name (max 50 characters)
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty

---

### UpdateEventStatusInput

Extends `CreateEventStatusInput` with:
- `id` (ID, required): Event status ID to update

---

## Response Types

### EventDetailResponse

Response for event mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `event` (Event, nullable): The created/updated event (null on error)

---

### EventTypeDetailResponse

Response for event type mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `eventType` (EventType, nullable): The created/updated event type (null on error)

---

### EventStatusDetailResponse

Response for event status mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `eventStatus` (EventStatus, nullable): The created/updated event status (null on error)

---

## Examples

### Complete Workflow Example

#### Step 1: Login
```graphql
mutation {
  tokenAuth(email: "user@example.com", password: "password") {
    token
    refreshToken
  }
}
```

#### Step 2: Create Event Type
```graphql
mutation {
  createEventType(input: {
    name: "Conference"
  }) {
    success
    message
    eventType {
      id
      name
    }
  }
}
```

#### Step 3: Create Event Status
```graphql
mutation {
  createEventStatus(input: {
    name: "Active"
  }) {
    success
    message
    eventStatus {
      id
      name
    }
  }
}
```

#### Step 4: Create Event
```graphql
mutation {
  createEvent(input: {
    name: "Annual Conference 2025"
    eventTypeId: "1"
    statusId: "1"
  }) {
    success
    message
    event {
      id
      uuid
      name
      eventType {
        name
      }
      status {
        name
      }
    }
  }
}
```

#### Step 5: Query Events
```graphql
query {
  events(limit: 10, offset: 0) {
    id
    name
    createdAt
    eventType {
      name
    }
    status {
      name
    }
  }
}
```

#### Step 6: Update Event
```graphql
mutation {
  updateEvent(input: {
    id: "1"
    name: "Updated Conference Name"
    eventTypeId: "1"
    statusId: "2"
  }) {
    success
    message
    event {
      id
      name
      updatedAt
    }
  }
}
```

---

## Error Handling

All mutations return a response object with `success` and `message` fields. On error:

- `success`: `false`
- `message`: Error description
- Related entity field: `null`

### Common Errors

1. **Validation Errors**
   ```json
   {
     "success": false,
     "message": "Validation errors: Name is required., Event type is required."
   }
   ```

2. **Authentication Required**
   ```json
   {
     "errors": [{
       "message": "User is not authenticated."
     }]
   }
   ```

3. **Not Found**
   ```json
   {
     "success": false,
     "message": "Event not found."
   }
   ```

4. **Tenant Access Error**
   ```json
   {
     "success": false,
     "message": "You don't have access to this tenant"
   }
   ```

---

## Role-Based Access

### Ambassadors & Clients
- Can only access events from their own tenant
- `tenantId` parameter is automatically set to their default tenant
- Cannot specify `tenantId` in mutations (will raise error)

### Spark Admin
- Can access events from any tenant
- Can specify `tenantId` in queries and mutations
- Has full CRUD access across all tenants

---

## Notes

1. **Pagination**: Use `limit` and `offset` for paginated queries
2. **Search**: Use the `q` parameter for case-insensitive name search
3. **Tenant Isolation**: Ambassadors and Clients are automatically limited to their tenant
4. **Validation**: All inputs are validated before processing
5. **Timestamps**: All timestamps are returned in ISO 8601 format

---

## Testing

### Using cURL

```bash
# Set your token
TOKEN="your_jwt_token_here"

# Create Event
curl -X POST http://localhost:8000/api/v1/graphql/ambassadors \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "query": "mutation { createEvent(input: { name: \"Test Event\", eventTypeId: \"1\", statusId: \"1\" }) { success message event { id name } } }"
  }'
```

### Using Python

```python
import requests

url = "http://localhost:8000/api/v1/graphql/ambassadors"
headers = {
    "Authorization": "Bearer your_token_here",
    "Content-Type": "application/json"
}

mutation = """
mutation {
  createEvent(input: {
    name: "Test Event"
    eventTypeId: "1"
    statusId: "1"
  }) {
    success
    message
    event {
      id
      name
    }
  }
}
"""

response = requests.post(url, json={"query": mutation}, headers=headers)
print(response.json())
```

---

## Support

For issues or questions, please refer to the main project documentation or contact the development team.

