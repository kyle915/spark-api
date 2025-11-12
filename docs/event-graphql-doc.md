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

Most endpoints require authentication. You must include a JWT token in the Authorization header:

```
Authorization: Bearer <your_jwt_token>
```

**Note**: The `createRequest` mutation is **public** and does not require authentication. All other mutations and queries require authentication.

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

### 6. Get Today's Events

Retrieve all events happening today for the authenticated user's tenant, optionally filtered by a search term. Events are returned in chronological order by `startTime`.

**Available for**: Ambassadors, Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  todayEvents(q: "training") {
    id
    uuid
    name
    startTime
    endTime
    address
    status {
      id
      name
    }
  }
}
```

**Parameters:**
- `q` (string, optional): Case-insensitive search string that filters by event name.

**Response:**
```json
{
  "data": {
    "todayEvents": [
      {
        "id": "12",
        "uuid": "91cb6f3a-3c5a-4daf-901a-661e95d8953d",
        "name": "In-Store Sampling",
        "startTime": "2025-01-20T14:00:00Z",
        "endTime": "2025-01-20T18:00:00Z",
        "address": "123 Market St, Springfield, IL",
        "status": {
          "id": "3",
          "name": "Scheduled"
        }
      }
    ]
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

### 7. Create Location

Create a new location.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createLocation(input: {
    name: "New York"
    code: "NY"
    zip: "10001"
    tenantId: "1"
  }) {
    success
    message
    location {
      id
      uuid
      name
      code
      zip
      tenantId
      createdAt
      updatedAt
    }
  }
}
```

**Input Fields:**
- `name` (string, required): Location name
- `code` (string, required): Location code
- `zip` (string, required): ZIP/postal code
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

**Response:**
```json
{
  "data": {
    "createLocation": {
      "success": true,
      "message": "Location created successfully.",
      "location": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "New York",
        "code": "NY",
        "zip": "10001",
        "tenantId": "1",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### 8. Update Location

Update an existing location.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateLocation(input: {
    id: "1"
    name: "Updated Location Name"
    code: "NYC"
    zip: "10002"
    tenantId: "1"
  }) {
    success
    message
    location {
      id
      uuid
      name
      code
      zip
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the location to update
- `name` (string, required): Updated location name
- `code` (string, required): Updated location code
- `zip` (string, required): Updated ZIP/postal code
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 9. Create Client

Create a new client.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createClient(input: {
    name: "Acme Corporation"
    email: "contact@acme.com"
    tenantId: "1"
  }) {
    success
    message
    client {
      id
      uuid
      name
      email
      tenantId
      createdAt
      updatedAt
    }
  }
}
```

**Input Fields:**
- `name` (string, required): Client name
- `email` (string, required): Client email address
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

**Response:**
```json
{
  "data": {
    "createClient": {
      "success": true,
      "message": "Client created successfully.",
      "client": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Acme Corporation",
        "email": "contact@acme.com",
        "tenantId": "1",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### 10. Update Client

Update an existing client.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateClient(input: {
    id: "1"
    name: "Updated Client Name"
    email: "newemail@acme.com"
    tenantId: "1"
  }) {
    success
    message
    client {
      id
      uuid
      name
      email
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the client to update
- `name` (string, required): Updated client name
- `email` (string, required): Updated client email address
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 11. Create Distributor

Create a new distributor.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createDistributor(input: {
    name: "ABC Distributors"
    email: "info@abcdist.com"
    locationId: "1"
    tenantId: "1"
  }) {
    success
    message
    distributor {
      id
      uuid
      name
      email
      tenantId
      location {
        id
        name
        code
      }
      createdAt
      updatedAt
    }
  }
}
```

**Input Fields:**
- `name` (string, required): Distributor name
- `email` (string, required): Distributor email address
- `locationId` (ID, required): ID of the associated location
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

**Response:**
```json
{
  "data": {
    "createDistributor": {
      "success": true,
      "message": "Distributor created successfully.",
      "distributor": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "ABC Distributors",
        "email": "info@abcdist.com",
        "tenantId": "1",
        "location": {
          "id": "1",
          "name": "New York",
          "code": "NY"
        },
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### 12. Update Distributor

Update an existing distributor.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateDistributor(input: {
    id: "1"
    name: "Updated Distributor Name"
    email: "newemail@abcdist.com"
    locationId: "2"
    tenantId: "1"
  }) {
    success
    message
    distributor {
      id
      uuid
      name
      email
      location {
        id
        name
      }
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the distributor to update
- `name` (string, required): Updated distributor name
- `email` (string, required): Updated distributor email address
- `locationId` (ID, required): Updated location ID
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 13. Create Retailer

Create a new retailer.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createRetailer(input: {
    name: "Best Buy Store"
    address: "123 Main St, New York, NY 10001"
    storeContact: "555-1234"
    locationId: "1"
    tenantId: "1"
  }) {
    success
    message
    retailer {
      id
      uuid
      name
      address
      storeContact
      tenantId
      location {
        id
        name
        code
      }
      createdAt
      updatedAt
    }
  }
}
```

**Input Fields:**
- `name` (string, required): Retailer name
- `address` (string, required): Retailer address
- `storeContact` (string, required): Store contact information
- `locationId` (ID, required): ID of the associated location
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

**Response:**
```json
{
  "data": {
    "createRetailer": {
      "success": true,
      "message": "Retailer created successfully.",
      "retailer": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Best Buy Store",
        "address": "123 Main St, New York, NY 10001",
        "storeContact": "555-1234",
        "tenantId": "1",
        "location": {
          "id": "1",
          "name": "New York",
          "code": "NY"
        },
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### 14. Update Retailer

Update an existing retailer.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateRetailer(input: {
    id: "1"
    name: "Updated Retailer Name"
    address: "456 New St, New York, NY 10002"
    storeContact: "555-5678"
    locationId: "2"
    tenantId: "1"
  }) {
    success
    message
    retailer {
      id
      uuid
      name
      address
      storeContact
      location {
        id
        name
      }
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the retailer to update
- `name` (string, required): Updated retailer name
- `address` (string, required): Updated retailer address
- `storeContact` (string, required): Updated store contact information
- `locationId` (ID, required): Updated location ID
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 15. Create Product Type

Create a new product type.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createProductType(input: {
    name: "Electronics"
    tenantId: "1"
  }) {
    success
    message
    productType {
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
- `name` (string, required): Product type name
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

**Response:**
```json
{
  "data": {
    "createProductType": {
      "success": true,
      "message": "Product type created successfully.",
      "productType": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Electronics",
        "tenantId": "1",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### 16. Update Product Type

Update an existing product type.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateProductType(input: {
    id: "1"
    name: "Updated Product Type Name"
    tenantId: "1"
  }) {
    success
    message
    productType {
      id
      uuid
      name
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the product type to update
- `name` (string, required): Updated product type name
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 17. Create Product

Create a new product.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createProduct(input: {
    name: "iPhone 15"
    productTypeId: "1"
    tenantId: "1"
  }) {
    success
    message
    product {
      id
      uuid
      name
      tenantId
      productType {
        id
        name
      }
      createdAt
      updatedAt
    }
  }
}
```

**Input Fields:**
- `name` (string, required): Product name
- `productTypeId` (ID, required): ID of the associated product type
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

**Response:**
```json
{
  "data": {
    "createProduct": {
      "success": true,
      "message": "Product created successfully.",
      "product": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "iPhone 15",
        "tenantId": "1",
        "productType": {
          "id": "1",
          "name": "Electronics"
        },
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### 18. Update Product

Update an existing product.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateProduct(input: {
    id: "1"
    name: "iPhone 15 Pro"
    productTypeId: "1"
    tenantId: "1"
  }) {
    success
    message
    product {
      id
      uuid
      name
      productType {
        id
        name
      }
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the product to update
- `name` (string, required): Updated product name
- `productTypeId` (ID, required): Updated product type ID
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 19. Create Request Type

Create a new request type.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createRequestType(input: {
    name: "Delivery Request"
    tenantId: "1"
  }) {
    success
    message
    requestType {
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
- `name` (string, required): Request type name
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

**Response:**
```json
{
  "data": {
    "createRequestType": {
      "success": true,
      "message": "Request type created successfully.",
      "requestType": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Delivery Request",
        "tenantId": "1",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### 20. Update Request Type

Update an existing request type.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateRequestType(input: {
    id: "1"
    name: "Updated Request Type Name"
    tenantId: "1"
  }) {
    success
    message
    requestType {
      id
      uuid
      name
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the request type to update
- `name` (string, required): Updated request type name
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 21. Create Request

Create a new request. **This mutation is PUBLIC** - no authentication required.

**Available for**: Public (no authentication required), Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation CreateRequest($input: CreateRequestInput!) {
  createRequest(input: $input) {
    success
    message
    request {
      id
      uuid
      name
      date
      startTime
      endTime
      address
      coordinates
      status {
        id
        name
        isDefault
      }
      tenantId
      client {
        id
        name
        email
      }
      distributor {
        id
        name
        email
      }
      retailer {
        id
        name
        address
      }
      requestType {
        id
        name
      }
      createdAt
      updatedAt
    }
  }
}
```

**Variables:**
```json
{
  "input": {
    "name": "Product Delivery Request",
    "date": "2025-01-20",
    "startTime": "2025-01-20T14:00:00Z",
    "endTime": "2025-01-20T16:00:00Z",
    "address": "123 Main St, New York, NY 10001",
    "coordinates": [-74.0060, 40.7128],
    "clientId": "1",
    "distributorId": "2",
    "retailerId": "3",
    "requestTypeId": "4",
    "tenantId": "5"
  }
}
```

**Input Fields:**
- `name` (string, required): Request name
- `date` (string, required): Request date (ISO 8601 format)
- `address` (string, required): Delivery/service address
- `coordinates` (List[float], required): Geographic coordinates `[longitude, latitude]`
- `clientId` (ID, required): ID of the associated client
- `distributorId` (ID, required): ID of the associated distributor
- `retailerId` (ID, required): ID of the associated retailer
- `requestTypeId` (ID, required): ID of the request type
- `tenantId` (ID, required): Tenant ID (required for public requests)

**Response:**
```json
{
  "data": {
    "createRequest": {
      "success": true,
      "message": "Request created successfully.",
      "request": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
      "name": "Product Delivery Request",
      "date": "2025-01-20",
      "startTime": "2025-01-20T14:00:00Z",
      "endTime": "2025-01-20T16:00:00Z",
      "address": "123 Main St, New York, NY 10001",
      "coordinates": [-74.0060, 40.7128],
      "status": {
        "id": "7",
        "name": "Pending",
        "isDefault": true
      },
        "tenantId": "5",
        "client": {
          "id": "1",
          "name": "Acme Corporation",
          "email": "contact@acme.com"
        },
        "distributor": {
          "id": "2",
          "name": "ABC Distributors",
          "email": "info@abcdist.com"
        },
        "retailer": {
          "id": "3",
          "name": "Best Buy Store",
          "address": "123 Main St, New York, NY 10001"
        },
        "requestType": {
          "id": "4",
          "name": "Delivery Request"
        },
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

**Note**: This is a public mutation that does not require authentication. For public requests, the `tenantId` field is required.

**Default status behavior**: If the tenant has a default `RequestStatus`, the system attaches it automatically when the request is created. If no default status exists, the `status` field remains `null`.

---

### 22. Update Request

Update an existing request.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateRequest(input: {
    id: "1"
    name: "Updated Request Name"
    date: "2025-01-25"
    startTime: "2025-01-25T15:00:00Z"
    endTime: "2025-01-25T17:00:00Z"
    address: "456 New St, New York, NY 10002"
    coordinates: [-74.0050, 40.7130]
    clientId: "1"
    distributorId: "2"
    retailerId: "3"
    requestTypeId: "4"
    tenantId: "5"
  }) {
    success
    message
    request {
      id
      uuid
      name
      date
      startTime
      endTime
      address
      coordinates
      status {
        id
        name
      }
      client {
        id
        name
      }
      distributor {
        id
        name
      }
      retailer {
        id
        name
      }
      requestType {
        id
        name
      }
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the request to update
- `name` (string, required): Updated request name
- `date` (string, required): Updated request date
- `startTime` (string, required): Updated start time (ISO 8601)
- `endTime` (string, required): Updated end time (ISO 8601)
- `address` (string, required): Updated delivery/service address
- `coordinates` (List[float], required): Updated geographic coordinates `[longitude, latitude]`
- `clientId` (ID, required): Updated client ID
- `distributorId` (ID, required): Updated distributor ID
- `retailerId` (ID, required): Updated retailer ID
- `requestTypeId` (ID, required): Updated request type ID
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)

---

### 23. Approve Request

Approve a pending request and (optionally) create a follow-up event when the approval status is configured to do so.

**Available for**: Authenticated users with permissions (Ambassadors cannot approve requests).

**GraphQL Mutation:**
```graphql
mutation {
  approveRequest(id: "1") {
    success
    message
    request {
      id
      name
      status {
        id
        name
      }
    }
    event {
      id
      startTime
      endTime
      status {
        id
        name
      }
    }
  }
}
```

**Behavior:**
- Requires authentication; the caller must belong to a tenant and have the appropriate role.
- Uses the tenant’s approval-status configuration (`RequestStatus.create_event` and `RequestStatus.is_default`) to determine the new request status.
- When the approval status is configured with `createEvent = true`, an `Event` is generated from the request and returned alongside the updated request.

---

## Types

### Event

Represents an event in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Event name
- `startTime` (string, nullable): Event start timestamp (ISO 8601)
- `endTime` (string, nullable): Event end timestamp (ISO 8601)
- `address` (string): Event address
- `isNational` (boolean): Whether the event is national
- `notes` (string, nullable): Additional notes attached to the event
- `request` (Request, nullable): Associated request when created from a request approval
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

### Location

Represents a location in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Location name
- `code` (string): Location code
- `zip` (string): ZIP/postal code
- `tenantId` (ID): Tenant ID
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### Client

Represents a client in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Client name
- `email` (string): Client email address
- `tenantId` (ID): Tenant ID
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### Distributor

Represents a distributor in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Distributor name
- `email` (string): Distributor email address
- `tenantId` (ID): Tenant ID
- `location` (Location, nullable): Associated location
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### Retailer

Represents a retailer in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Retailer name
- `address` (string): Retailer address
- `storeContact` (string): Store contact information
- `tenantId` (ID): Tenant ID
- `location` (Location, nullable): Associated location
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### ProductType

Represents a product type in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Product type name
- `tenantId` (ID): Tenant ID
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### Product

Represents a product in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Product name
- `tenantId` (ID): Tenant ID
- `productType` (ProductType, nullable): Associated product type
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### RequestType

Represents a request type in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Request type name
- `tenantId` (ID): Tenant ID
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### RequestStatus

Represents a status value that can be assigned to requests.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Status name
- `createEvent` (boolean): Whether approving with this status should spawn an event
- `isDefault` (boolean): Marks the tenant-wide default status applied on create
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### Request

Represents a request in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Request name
- `date` (string): Request date
- `startTime` (string, nullable): Request start time (ISO 8601)
- `endTime` (string, nullable): Request end time (ISO 8601)
- `address` (string): Delivery/service address
- `coordinates` (List[float]): Geographic coordinates `[longitude, latitude]`
- `status` (RequestStatus, nullable): Current request status
- `tenantId` (ID): Tenant ID
- `client` (Client, nullable): Associated client
- `distributor` (Distributor, nullable): Associated distributor
- `retailer` (Retailer, nullable): Associated retailer
- `requestType` (RequestType, nullable): Associated request type
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

### CreateLocationInput

**Fields:**
- `name` (string, required): Location name
- `code` (string, required): Location code
- `zip` (string, required): ZIP/postal code
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty
- Code is required and cannot be empty
- ZIP is required and cannot be empty

---

### UpdateLocationInput

Extends `CreateLocationInput` with:
- `id` (ID, required): Location ID to update

---

### CreateClientInput

**Fields:**
- `name` (string, required): Client name
- `email` (string, required): Client email address
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty
- Email is required and must be a valid email format

---

### UpdateClientInput

Extends `CreateClientInput` with:
- `id` (ID, required): Client ID to update

---

### CreateDistributorInput

**Fields:**
- `name` (string, required): Distributor name
- `email` (string, required): Distributor email address
- `locationId` (ID, required): ID of the associated location
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty
- Email is required and must be a valid email format
- Location ID is required

---

### UpdateDistributorInput

Extends `CreateDistributorInput` with:
- `id` (ID, required): Distributor ID to update

---

### CreateRetailerInput

**Fields:**
- `name` (string, required): Retailer name
- `address` (string, required): Retailer address
- `storeContact` (string, required): Store contact information
- `locationId` (ID, required): ID of the associated location
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty
- Address is required and cannot be empty
- Store contact is required and cannot be empty
- Location ID is required

---

### UpdateRetailerInput

Extends `CreateRetailerInput` with:
- `id` (ID, required): Retailer ID to update

---

### CreateProductTypeInput

**Fields:**
- `name` (string, required): Product type name
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty

---

### UpdateProductTypeInput

Extends `CreateProductTypeInput` with:
- `id` (ID, required): Product type ID to update

---

### CreateProductInput

**Fields:**
- `name` (string, required): Product name
- `productTypeId` (ID, required): ID of the associated product type
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty
- Product type ID is required

---

### UpdateProductInput

Extends `CreateProductInput` with:
- `id` (ID, required): Product ID to update

---

### CreateRequestTypeInput

**Fields:**
- `name` (string, required): Request type name
- `tenantId` (ID, optional): Tenant ID (only for Spark Admin)

**Validation:**
- Name is required and cannot be empty

---

### UpdateRequestTypeInput

Extends `CreateRequestTypeInput` with:
- `id` (ID, required): Request type ID to update

---

### CreateRequestInput

**Fields:**
- `name` (string, required): Request name
- `date` (string, required): Request date (ISO 8601 format)
- `startTime` (string, required): Request start time (ISO 8601 format)
- `endTime` (string, required): Request end time (ISO 8601 format)
- `address` (string, required): Delivery/service address
- `coordinates` (List[float], required): Geographic coordinates `[longitude, latitude]`
- `clientId` (ID, required): ID of the associated client
- `distributorId` (ID, required): ID of the associated distributor
- `retailerId` (ID, required): ID of the associated retailer
- `requestTypeId` (ID, required): ID of the request type
- `tenantId` (ID, required): Tenant ID (required for public requests)

**Validation:**
- Name is required and cannot be empty
- Date, startTime, and endTime are required and must be valid ISO 8601 strings
- Address is required and cannot be empty
- Coordinates must be a list of exactly 2 floats `[longitude, latitude]`
- All ID fields (clientId, distributorId, retailerId, requestTypeId) are required
- Tenant ID is required for public (unauthenticated) requests

---

### UpdateRequestInput

Extends `CreateRequestInput` with:
- `id` (ID, required): Request ID to update

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

### LocationDetailResponse

Response for location mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `location` (Location, nullable): The created/updated location (null on error)

---

### ClientDetailResponse

Response for client mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `client` (Client, nullable): The created/updated client (null on error)

---

### DistributorDetailResponse

Response for distributor mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `distributor` (Distributor, nullable): The created/updated distributor (null on error)

---

### RetailerDetailResponse

Response for retailer mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `retailer` (Retailer, nullable): The created/updated retailer (null on error)

---

### ProductTypeDetailResponse

Response for product type mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `productType` (ProductType, nullable): The created/updated product type (null on error)

---

### ProductDetailResponse

Response for product mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `product` (Product, nullable): The created/updated product (null on error)

---

### RequestTypeDetailResponse

Response for request type mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `requestType` (RequestType, nullable): The created/updated request type (null on error)

---

### RequestDetailResponse

Response for request mutations.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `request` (Request, nullable): The created/updated request (null on error)

---

### ApproveRequestResponse

Response returned by the `approveRequest` mutation.

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `request` (Request, nullable): The updated request after approval
- `event` (Event, nullable): The event created from the request (present only when the approval status is configured to spawn events)

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

