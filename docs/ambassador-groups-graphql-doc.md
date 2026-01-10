# Group Types and Ambassador Groups GraphQL API Documentation

This document provides comprehensive documentation for the Group Types and Ambassador Groups GraphQL API endpoints.

## Table of Contents

- [Base URLs](#base-urls)
- [Authentication](#authentication)
- [Queries](#queries)
  - [Group Types](#group-types)
  - [Ambassador Groups](#ambassador-groups)
- [Mutations](#mutations)
  - [Group Types](#group-types-mutations)
  - [Ambassador Groups](#ambassador-groups-mutations)
- [Types](#types)
- [Input Types](#input-types)
- [Response Types](#response-types)
- [Special Features](#special-features)
- [Error Handling](#error-handling)
- [Examples](#examples)

---

## Base URLs

The API provides GraphQL endpoints on the following schemas:

- **Spark Admin**: `http://localhost:8000/api/v1/graphql/spark`
- **Clients**: `http://localhost:8000/api/v1/graphql/clients`

**Note**: Group Types and Ambassador Groups operations are **not available** on the Ambassadors schema. These endpoints require `IsClientOrSparkAdmin` permission.

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

All list queries use **Relay-style pagination** with the following parameters:

- `first` (int, optional): Number of items to return from the start
- `after` (string, optional): Cursor for pagination (from previous query)
- `last` (int, optional): Number of items to return from the end
- `before` (string, optional): Cursor for pagination (from previous query)
- `filters` (object, optional): Filtering options (includes `search` for name filtering)

All list queries return a `CountableConnection` type with the following structure:

```graphql
{
  edges {
    node {
      # Your fields here
    }
    cursor
  }
  pageInfo {
    hasNextPage
    hasPreviousPage
    startCursor
    endCursor
  }
  totalCount
}
```

---

### Group Types

#### Get Group Types

Retrieve a paginated list of group types.

**Available for**: Spark Admin, Clients

**GraphQL Query:**
```graphql
query {
  groupTypes(
    first: 10
    after: null
    filters: { search: "marketing" }
  ) {
    edges {
      node {
        id
        uuid
        name
        createdAt
        updatedAt
      }
      cursor
    }
    pageInfo {
      hasNextPage
      hasPreviousPage
      startCursor
      endCursor
    }
    totalCount
  }
}
```

**Response:**
```json
{
  "data": {
    "groupTypes": {
      "edges": [
        {
          "node": {
            "id": "R3JvdXBUeXBlOjE=",
            "uuid": "01234567-89ab-cdef-0123-456789abcdef",
            "name": "Marketing Team",
            "createdAt": "2025-01-15T10:00:00Z",
            "updatedAt": "2025-01-15T10:00:00Z"
          },
          "cursor": "eyJvcmRlcl9ieSI6Ii1jcmVhdGVkX2F0IiwiaWRfX2d0Ijo3OX0="
        }
      ],
      "pageInfo": {
        "hasNextPage": false,
        "hasPreviousPage": false,
        "startCursor": "eyJvcmRlcl9ieSI6Ii1jcmVhdGVkX2F0IiwiaWRfX2d0Ijo3OX0=",
        "endCursor": "eyJvcmRlcl9ieSI6Ii1jcmVhdGVkX2F0IiwiaWRfX2d0Ijo3OX0="
      },
      "totalCount": 1
    }
  }
}
```

---

#### Get Single Group Type

Retrieve a single group type by ID.

**Available for**: Spark Admin, Clients

**GraphQL Query:**
```graphql
query {
  groupType(groupTypeId: "R3JvdXBUeXBlOjE=") {
    id
    uuid
    name
    createdAt
    updatedAt
  }
}
```

**Response:**
```json
{
  "data": {
    "groupType": {
      "id": "R3JvdXBlOjE=",
      "uuid": "01234567-89ab-cdef-0123-456789abcdef",
      "name": "Marketing Team",
      "createdAt": "2025-01-15T10:00:00Z",
      "updatedAt": "2025-01-15T10:00:00Z"
    }
  }
}
```

---

### Ambassador Groups

#### Get Ambassador Groups

Retrieve a paginated list of ambassador groups for the authenticated user's tenant.

**Available for**: Spark Admin, Clients

**GraphQL Query:**
```graphql
query {
  ambassadorGroups(
    first: 10
    after: null
    filters: { search: "sales" }
  ) {
    edges {
      node {
        id
        uuid
        name
        description
        private
        groupType {
          id
          name
        }
        tenantId
        members {
          id
          uuid
          user {
            id
            email
            username
          }
          ambassador {
            id
            uuid
          }
        }
        createdAt
        updatedAt
      }
      cursor
    }
    pageInfo {
      hasNextPage
      hasPreviousPage
      startCursor
      endCursor
    }
    totalCount
  }
}
```

**Response:**
```json
{
  "data": {
    "ambassadorGroups": {
      "edges": [
        {
          "node": {
            "id": "QW1iYXNzYWRvckdyb3VwOjE=",
            "uuid": "01234567-89ab-cdef-0123-456789abcdef",
            "name": "Sales Team A",
            "description": "Main sales team",
            "private": false,
            "groupType": {
              "id": "R3JvdXBUeXBlOjE=",
              "name": "Sales Team"
            },
            "tenantId": "MTI3",
            "members": [
              {
                "id": "VXNlckdyb3VwOjE=",
                "uuid": "abcdef12-3456-7890-abcd-ef1234567890",
                "user": {
                  "id": "VXNlcjox",
                  "email": "ambassador@example.com",
                  "username": "ambassador1"
                },
                "ambassador": {
                  "id": "QW1iYXNzYWRvcjox",
                  "uuid": "12345678-90ab-cdef-1234-567890abcdef"
                }
              }
            ],
            "createdAt": "2025-01-15T10:00:00Z",
            "updatedAt": "2025-01-15T10:00:00Z"
          },
          "cursor": "eyJvcmRlcl9ieSI6Ii1jcmVhdGVkX2F0IiwiaWRfX2FnIjo3OX0="
        }
      ],
      "pageInfo": {
        "hasNextPage": false,
        "hasPreviousPage": false,
        "startCursor": "eyJvcmRlcl9ieSI6Ii1jcmVhdGVkX2F0IiwiaWRfX2FnIjo3OX0=",
        "endCursor": "eyJvcmRlcl9ieSI6Ii1jcmVhdGVkX2F0IiwiaWRfX2FnIjo3OX0="
      },
      "totalCount": 1
    }
  }
}
```

---

#### Get Single Ambassador Group

Retrieve a single ambassador group by ID with its members.

**Available for**: Spark Admin, Clients

**GraphQL Query:**
```graphql
query {
  ambassadorGroup(groupId: "QW1iYXNzYWRvckdyb3VwOjE=") {
    id
    uuid
    name
    description
    private
    groupType {
      id
      name
    }
    tenantId
    members {
      id
      uuid
      user {
        id
        email
        username
      }
      ambassador {
        id
        uuid
      }
    }
    createdAt
    updatedAt
  }
}
```

**Response:**
```json
{
  "data": {
    "ambassadorGroup": {
      "id": "QW1iYXNzYWRvckdyb3VwOjE=",
      "uuid": "01234567-89ab-cdef-0123-456789abcdef",
      "name": "Sales Team A",
      "description": "Main sales team",
      "private": false,
      "groupType": {
        "id": "R3JvdXBUeXBlOjE=",
        "name": "Sales Team"
      },
      "tenantId": "MTI3",
      "members": [
        {
          "id": "VXNlckdyb3VwOjE=",
          "uuid": "abcdef12-3456-7890-abcd-ef1234567890",
          "user": {
            "id": "VXNlcjox",
            "email": "ambassador@example.com",
            "username": "ambassador1"
          },
          "ambassador": {
            "id": "QW1iYXNzYWRvcjox",
            "uuid": "12345678-90ab-cdef-1234-567890abcdef"
          }
        }
      ],
      "createdAt": "2025-01-15T10:00:00Z",
      "updatedAt": "2025-01-15T10:00:00Z"
    }
  }
}
```

---

## Mutations

All mutations follow the Relay mutation pattern and return a response object with `success`, `message`, and `clientMutationId` fields.

---

### Group Types Mutations

#### Create Group Type

Create a new group type.

**Available for**: Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createGroupType(input: {
    name: "Marketing Team"
    clientMutationId: "create-1"
  }) {
    success
    message
    clientMutationId
    groupType {
      id
      uuid
      name
      createdAt
      updatedAt
    }
  }
}
```

**Response:**
```json
{
  "data": {
    "createGroupType": {
      "success": true,
      "message": "Group type created successfully.",
      "clientMutationId": "create-1",
      "groupType": {
        "id": "R3JvdXBUeXBlOjE=",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Marketing Team",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

#### Update Group Type

Update an existing group type.

**Available for**: Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateGroupType(input: {
    id: "R3JvdXBUeXBlOjE="
    name: "Marketing Team Updated"
    clientMutationId: "update-1"
  }) {
    success
    message
    clientMutationId
    groupType {
      id
      uuid
      name
      createdAt
      updatedAt
    }
  }
}
```

**Response:**
```json
{
  "data": {
    "updateGroupType": {
      "success": true,
      "message": "Group type updated successfully.",
      "clientMutationId": "update-1",
      "groupType": {
        "id": "R3JvdXBUeXBlOjE=",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Marketing Team Updated",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:30:00Z"
      }
    }
  }
}
```

---

#### Delete Group Type

Delete a group type.

**Available for**: Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  deleteGroupType(input: {
    id: "R3JvdXBUeXBlOjE="
    clientMutationId: "delete-1"
  }) {
    success
    message
    clientMutationId
  }
}
```

**Response:**
```json
{
  "data": {
    "deleteGroupType": {
      "success": true,
      "message": "Group type deleted successfully.",
      "clientMutationId": "delete-1",
      "groupType": null
    }
  }
}
```

---

### Ambassador Groups Mutations

#### Create Ambassador Group

Create a new ambassador group. This mutation validates that the job exists and has a rate assigned. Optionally, it can create `UserGroup` records and `AmbassadorJob` invitations for ambassadors.

**Available for**: Spark Admin, Clients

**GraphQL Mutation (without ambassadors):**
```graphql
mutation {
  createAmbassadorGroup(input: {
    name: "Sales Team A"
    tenantId: "MTI3"
    jobId: "Sm9iOjE="
    groupTypeId: "R3JvdXBUeXBlOjE="
    description: "Main sales team"
    private: false
    clientMutationId: "create-ag-1"
  }) {
    success
    message
    clientMutationId
    ambassadorGroup {
      id
      uuid
      name
      description
      private
      groupType {
        id
        name
      }
      tenantId
      members {
        id
      }
      createdAt
      updatedAt
    }
  }
}
```

**GraphQL Mutation (with ambassadors):**
```graphql
mutation {
  createAmbassadorGroup(input: {
    name: "Sales Team A"
    tenantId: "MTI3"
    jobId: "Sm9iOjE="
    groupTypeId: "R3JvdXBlOjE="
    description: "Main sales team"
    private: false
    ambassadorIds: [
      "QW1iYXNzYWRvcjox",
      "QW1iYXNzYWRvcjoy"
    ]
    clientMutationId: "create-ag-2"
  }) {
    success
    message
    clientMutationId
    ambassadorGroup {
      id
      uuid
      name
      description
      private
      groupType {
        id
        name
      }
      tenantId
      members {
        id
        uuid
        user {
          id
          email
        }
        ambassador {
          id
          uuid
        }
      }
      createdAt
      updatedAt
    }
  }
}
```

**Response (with ambassadors):**
```json
{
  "data": {
    "createAmbassadorGroup": {
      "success": true,
      "message": "Ambassador group created successfully.",
      "clientMutationId": "create-ag-2",
      "ambassadorGroup": {
        "id": "QW1iYXNzYWRvckdyb3VwOjE=",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Sales Team A",
        "description": "Main sales team",
        "private": false,
        "groupType": {
          "id": "R3JvdXBUeXBlOjE=",
          "name": "Sales Team"
        },
        "tenantId": "MTI3",
        "members": [
          {
            "id": "VXNlckdyb3VwOjE=",
            "uuid": "abcdef12-3456-7890-abcd-ef1234567890",
            "user": {
              "id": "VXNlcjox",
              "email": "ambassador1@example.com"
            },
            "ambassador": {
              "id": "QW1iYXNzYWRvcjox",
              "uuid": "12345678-90ab-cdef-1234-567890abcdef"
            }
          },
          {
            "id": "VXNlckdyb3VwOjI=",
            "uuid": "bcdef123-4567-8901-bcde-f12345678901",
            "user": {
              "id": "VXNlcjoy",
              "email": "ambassador2@example.com"
            },
            "ambassador": {
              "id": "QW1iYXNzYWRvcjoy",
              "uuid": "23456789-01bc-def2-3456-789012345678"
            }
          }
        ],
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

**Special Behavior:**
- Validates that the job exists
- Validates that the job has a rate assigned
- If `ambassadorIds` are provided:
  - Creates `UserGroup` records linking each ambassador to the group
  - Creates `AmbassadorJob` records with "invited" status for each ambassador
  - Assigns the job's rate to each `AmbassadorJob`
  - If the "invited" status doesn't exist, it will be created automatically

---

#### Update Ambassador Group

Update an existing ambassador group.

**Available for**: Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateAmbassadorGroup(input: {
    id: "QW1iYXNzYWRvckdyb3VwOjE="
    name: "Sales Team A Updated"
    description: "Updated description"
    private: true
    groupTypeId: "R3JvdXBUeXBlOjI="
    clientMutationId: "update-ag-1"
  }) {
    success
    message
    clientMutationId
    ambassadorGroup {
      id
      uuid
      name
      description
      private
      groupType {
        id
        name
      }
      tenantId
      createdAt
      updatedAt
    }
  }
}
```

**Response:**
```json
{
  "data": {
    "updateAmbassadorGroup": {
      "success": true,
      "message": "Ambassador group updated successfully.",
      "clientMutationId": "update-ag-1",
      "ambassadorGroup": {
        "id": "QW1iYXNzYWRvckdyb3VwOjE=",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Sales Team A Updated",
        "description": "Updated description",
        "private": true,
        "groupType": {
          "id": "R3JvdXBUeXBlOjI=",
          "name": "Sales Team Updated"
        },
        "tenantId": "MTI3",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:30:00Z"
      }
    }
  }
}
```

---

#### Delete Ambassador Group

Delete an ambassador group.

**Available for**: Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  deleteAmbassadorGroup(input: {
    id: "QW1iYXNzYWRvckdyb3VwOjE="
    clientMutationId: "delete-ag-1"
  }) {
    success
    message
    clientMutationId
  }
}
```

**Response:**
```json
{
  "data": {
    "deleteAmbassadorGroup": {
      "success": true,
      "message": "Ambassador group deleted successfully.",
      "clientMutationId": "delete-ag-1",
      "ambassadorGroup": null
    }
  }
}
```

---

## Types

### GroupType

Represents a group type/category for organizing ambassador groups.

**Fields:**
- `id` (ID!): Relay global ID
- `uuid` (String!): Unique identifier
- `name` (String!): Group type name
- `createdAt` (String!): Creation timestamp (ISO 8601)
- `updatedAt` (String!): Last update timestamp (ISO 8601)

**Example:**
```graphql
{
  id: "R3JvdXBUeXBlOjE="
  uuid: "01234567-89ab-cdef-0123-456789abcdef"
  name: "Marketing Team"
  createdAt: "2025-01-15T10:00:00Z"
  updatedAt: "2025-01-15T10:00:00Z"
}
```

---

### AmbassadorGroup

Represents a group of ambassadors associated with a job and group type.

**Fields:**
- `id` (ID!): Relay global ID
- `uuid` (String!): Unique identifier
- `name` (String!): Group name
- `description` (String): Group description (optional)
- `private` (Boolean!): Whether the group is private
- `groupType` (GroupType!): Associated group type
- `tenantId` (ID!): Tenant ID (Relay global ID)
- `members` ([UserGroup!]!): List of user group members
- `createdAt` (String!): Creation timestamp (ISO 8601)
- `updatedAt` (String!): Last update timestamp (ISO 8601)

**Example:**
```graphql
{
  id: "QW1iYXNzYWRvckdyb3VwOjE="
  uuid: "01234567-89ab-cdef-0123-456789abcdef"
  name: "Sales Team A"
  description: "Main sales team"
  private: false
  groupType: {
    id: "R3JvdXBUeXBlOjE="
    name: "Sales Team"
  }
  tenantId: "MTI3"
  members: [
    {
      id: "VXNlckdyb3VwOjE="
      uuid: "abcdef12-3456-7890-abcd-ef1234567890"
      user: {
        id: "VXNlcjox"
        email: "ambassador@example.com"
      }
      ambassador: {
        id: "QW1iYXNzYWRvcjox"
      }
    }
  ]
  createdAt: "2025-01-15T10:00:00Z"
  updatedAt: "2025-01-15T10:00:00Z"
}
```

---

### UserGroup

Represents a user's membership in an ambassador group.

**Fields:**
- `id` (ID!): Relay global ID
- `uuid` (String!): Unique identifier
- `user` (SparkUserType!): Associated user
- `ambassador` (Ambassador): Associated ambassador (optional)

**Example:**
```graphql
{
  id: "VXNlckdyb3VwOjE="
  uuid: "abcdef12-3456-7890-abcd-ef1234567890"
  user: {
    id: "VXNlcjox"
    email: "ambassador@example.com"
    username: "ambassador1"
  }
  ambassador: {
    id: "QW1iYXNzYWRvcjox"
    uuid: "12345678-90ab-cdef-1234-567890abcdef"
  }
}
```

---

## Input Types

### CreateGroupTypeInput

Input for creating a group type.

**Fields:**
- `name` (String!): Group type name (required)
- `clientMutationId` (ID): Client mutation ID for tracking (optional)

**Example:**
```graphql
{
  name: "Marketing Team"
  clientMutationId: "create-1"
}
```

---

### UpdateGroupTypeInput

Input for updating a group type.

**Fields:**
- `id` (ID!): Group type ID (required)
- `name` (String!): Group type name (required)
- `clientMutationId` (ID): Client mutation ID for tracking (optional)

**Example:**
```graphql
{
  id: "R3JvdXBUeXBlOjE="
  name: "Marketing Team Updated"
  clientMutationId: "update-1"
}
```

---

### DeleteGroupTypeInput

Input for deleting a group type.

**Fields:**
- `id` (ID!): Group type ID (required)
- `clientMutationId` (ID): Client mutation ID for tracking (optional)

**Example:**
```graphql
{
  id: "R3JvdXBUeXBlOjE="
  clientMutationId: "delete-1"
}
```

---

### GroupTypeFiltersInput

Filters for group type queries.

**Fields:**
- `search` (String): Search query to filter by name (case-insensitive, optional)

**Example:**
```graphql
{
  search: "marketing"
}
```

---

### CreateAmbassadorGroupInput

Input for creating an ambassador group.

**Fields:**
- `name` (String!): Group name (required)
- `tenantId` (ID!): Tenant ID (required)
- `jobId` (ID!): Job ID - must exist and have a rate assigned (required)
- `groupTypeId` (ID!): Group type ID (required)
- `description` (String): Group description (optional)
- `private` (Boolean): Whether the group is private (optional, default: false)
- `ambassadorIds` ([ID!]): List of ambassador IDs to add to the group (optional)
- `clientMutationId` (ID): Client mutation ID for tracking (optional)

**Example:**
```graphql
{
  name: "Sales Team A"
  tenantId: "MTI3"
  jobId: "Sm9iOjE="
  groupTypeId: "R3JvdXBUeXBlOjE="
  description: "Main sales team"
  private: false
  ambassadorIds: [
    "QW1iYXNzYWRvcjox",
    "QW1iYXNzYWRvcjoy"
  ]
  clientMutationId: "create-ag-1"
}
```

---

### UpdateAmbassadorGroupInput

Input for updating an ambassador group.

**Fields:**
- `id` (ID!): Ambassador group ID (required)
- `name` (String): Group name (optional)
- `tenantId` (ID): Tenant ID (optional)
- `jobId` (ID): Job ID (optional)
- `groupTypeId` (ID): Group type ID (optional)
- `description` (String): Group description (optional)
- `private` (Boolean): Whether the group is private (optional)
- `ambassadorIds` ([ID!]): List of ambassador IDs (optional)
- `clientMutationId` (ID): Client mutation ID for tracking (optional)

**Example:**
```graphql
{
  id: "QW1iYXNzYWRvckdyb3VwOjE="
  name: "Sales Team A Updated"
  description: "Updated description"
  private: true
  clientMutationId: "update-ag-1"
}
```

---

### DeleteAmbassadorGroupInput

Input for deleting an ambassador group.

**Fields:**
- `id` (ID!): Ambassador group ID (required)
- `clientMutationId` (ID): Client mutation ID for tracking (optional)

**Example:**
```graphql
{
  id: "QW1iYXNzYWRvckdyb3VwOjE="
  clientMutationId: "delete-ag-1"
}
```

---

### AmbassadorGroupFiltersInput

Filters for ambassador group queries.

**Fields:**
- `search` (String): Search query to filter by name (case-insensitive, optional)

**Example:**
```graphql
{
  search: "sales"
}
```

---

## Response Types

### GroupTypeResponse

Response type for group type mutations.

**Fields:**
- `success` (Boolean!): Whether the operation was successful
- `message` (String!): Response message
- `clientMutationId` (ID): Client mutation ID (if provided)
- `groupType` (GroupType): The group type object (null on deletion or error)

**Example:**
```json
{
  "success": true,
  "message": "Group type created successfully.",
  "clientMutationId": "create-1",
  "groupType": {
    "id": "R3JvdXBUeXBlOjE=",
    "uuid": "01234567-89ab-cdef-0123-456789abcdef",
    "name": "Marketing Team",
    "createdAt": "2025-01-15T10:00:00Z",
    "updatedAt": "2025-01-15T10:00:00Z"
  }
}
```

---

### AmbassadorGroupResponse

Response type for ambassador group mutations.

**Fields:**
- `success` (Boolean!): Whether the operation was successful
- `message` (String!): Response message
- `clientMutationId` (ID): Client mutation ID (if provided)
- `ambassadorGroup` (AmbassadorGroup): The ambassador group object (null on deletion or error)

**Example:**
```json
{
  "success": true,
  "message": "Ambassador group created successfully.",
  "clientMutationId": "create-ag-1",
  "ambassadorGroup": {
    "id": "QW1iYXNzYWRvckdyb3VwOjE=",
    "uuid": "01234567-89ab-cdef-0123-456789abcdef",
    "name": "Sales Team A",
    "description": "Main sales team",
    "private": false,
    "groupType": {
      "id": "R3JvdXBUeXBlOjE=",
      "name": "Sales Team"
    },
    "tenantId": "MTI3",
    "createdAt": "2025-01-15T10:00:00Z",
    "updatedAt": "2025-01-15T10:00:00Z"
  }
}
```

---

## Special Features

### Create Ambassador Group with Job Validation and Ambassador Invitations

The `createAmbassadorGroup` mutation includes special business logic:

#### 1. Job Validation
- Validates that the job exists
- Validates that the job has a rate assigned
- Returns an error if either validation fails

#### 2. Automatic UserGroup Creation
When `ambassadorIds` are provided:
- Creates a `UserGroup` record for each ambassador
- Links the ambassador's user to the newly created group
- Links the ambassador record to the group

#### 3. Automatic AmbassadorJob Creation
When `ambassadorIds` are provided:
- Creates an `AmbassadorJob` record for each ambassador
- Sets the status to "invited"
- Creates the "invited" status if it doesn't exist
- Assigns the job's rate to each `AmbassadorJob`
- Sets `appear_as_rfp` to `true`

#### 4. Atomic Transaction
All operations (group creation, UserGroup creation, AmbassadorJob creation) are executed within a single database transaction, ensuring data consistency.

**Example Flow:**
```
1. Validate job exists and has rate
2. Create AmbassadorGroup
3. For each ambassador_id:
   a. Create UserGroup (group, user, ambassador)
   b. Create AmbassadorJob (ambassador, job, status="invited", rate=job.rate)
4. Return created AmbassadorGroup with members
```

**Error Scenarios:**
- Job not found: Returns `success: false` with message "Job not found."
- Job without rate: Returns `success: false` with message "Job must have a rate assigned."
- Ambassador not found: Returns `success: false` with message "Ambassadors with IDs {missing_ids} not found."
- Invalid ambassador ID format: Returns `success: false` with message "Invalid ambassador ID: {id}"

---

## Error Handling

All mutations return a response object with `success` and `message` fields. On error:

- `success`: `false`
- `message`: Error description
- Related entity field: `null`

### Common Errors

#### 1. Permission Errors

**Ambassador users cannot access:**
```json
{
  "errors": [{
    "message": "You do not have permission to perform this action. Client or Spark Admin access required.",
    "locations": [{"line": 2, "column": 3}],
    "path": ["groupTypes"]
  }]
}
```

#### 2. Validation Errors

**Missing required field:**
```json
{
  "data": {
    "createAmbassadorGroup": {
      "success": false,
      "message": "Job ID is required.",
      "ambassadorGroup": null
    }
  }
}
```

**GraphQL validation error (missing required field):**
```json
{
  "errors": [{
    "message": "Variable '$input' got invalid value {'name': 'Group Without Job', 'tenantId': '127', 'groupTypeId': '156'}; Field 'jobId' of required type 'ID!' was not provided.",
    "locations": [{"line": 2, "column": 44}]
  }]
}
```

#### 3. Business Logic Errors

**Job not found:**
```json
{
  "data": {
    "createAmbassadorGroup": {
      "success": false,
      "message": "Job not found.",
      "ambassadorGroup": null
    }
  }
}
```

**Job without rate:**
```json
{
  "data": {
    "createAmbassadorGroup": {
      "success": false,
      "message": "Job must have a rate assigned.",
      "ambassadorGroup": null
    }
  }
}
```

**Ambassadors not found:**
```json
{
  "data": {
    "createAmbassadorGroup": {
      "success": false,
      "message": "Ambassadors with IDs {1, 2} not found.",
      "ambassadorGroup": null
    }
  }
}
```

**Invalid ID format:**
```json
{
  "data": {
    "createAmbassadorGroup": {
      "success": false,
      "message": "Invalid job ID: invalid-id",
      "ambassadorGroup": null
    }
  }
}
```

#### 4. Not Found Errors

**Group type not found:**
```json
{
  "data": {
    "updateGroupType": {
      "success": false,
      "message": "GroupType matching query does not exist.",
      "groupType": null
    }
  }
}
```

**Ambassador group not found:**
```json
{
  "data": {
    "updateAmbassadorGroup": {
      "success": false,
      "message": "AmbassadorGroup matching query does not exist.",
      "ambassadorGroup": null
    }
  }
}
```

#### 5. Authentication Required

```json
{
  "errors": [{
    "message": "User is not authenticated."
  }]
}
```

---

## Examples

### Complete Workflow Example

#### Step 1: Create a Group Type
```graphql
mutation {
  createGroupType(input: {
    name: "Sales Team"
    clientMutationId: "step-1"
  }) {
    success
    message
    groupType {
      id
      name
    }
  }
}
```

#### Step 2: Create an Ambassador Group (without ambassadors)
```graphql
mutation {
  createAmbassadorGroup(input: {
    name: "Sales Team A"
    tenantId: "MTI3"
    jobId: "Sm9iOjE="
    groupTypeId: "R3JvdXBUeXBlOjE="
    description: "Main sales team"
    private: false
    clientMutationId: "step-2"
  }) {
    success
    message
    ambassadorGroup {
      id
      name
      description
      groupType {
        name
      }
    }
  }
}
```

#### Step 3: Create an Ambassador Group (with ambassadors)
```graphql
mutation {
  createAmbassadorGroup(input: {
    name: "Sales Team B"
    tenantId: "MTI3"
    jobId: "Sm9iOjE="
    groupTypeId: "R3JvdXBUeXBlOjE="
    description: "Secondary sales team"
    private: false
    ambassadorIds: [
      "QW1iYXNzYWRvcjox",
      "QW1iYXNzYWRvcjoy"
    ]
    clientMutationId: "step-3"
  }) {
    success
    message
    ambassadorGroup {
      id
      name
      members {
        id
        user {
          email
        }
        ambassador {
          id
        }
      }
    }
  }
}
```

#### Step 4: Query Ambassador Groups with Members
```graphql
query {
  ambassadorGroups(first: 10) {
    edges {
      node {
        id
        name
        description
        groupType {
          name
        }
        members {
          id
          user {
            email
            username
          }
          ambassador {
            id
            uuid
          }
        }
      }
    }
    totalCount
  }
}
```

#### Step 5: Update an Ambassador Group
```graphql
mutation {
  updateAmbassadorGroup(input: {
    id: "QW1iYXNzYWRvckdyb3VwOjE="
    name: "Sales Team A Updated"
    description: "Updated description"
    private: true
    clientMutationId: "step-5"
  }) {
    success
    message
    ambassadorGroup {
      id
      name
      description
      private
    }
  }
}
```

#### Step 6: Delete an Ambassador Group
```graphql
mutation {
  deleteAmbassadorGroup(input: {
    id: "QW1iYXNzYWRvckdyb3VwOjE="
    clientMutationId: "step-6"
  }) {
    success
    message
  }
}
```

---

## Notes

1. **Relay Pagination**: All list queries use Relay-style pagination with `first`, `after`, `last`, `before` parameters
2. **Search**: Use the `filters.search` parameter for case-insensitive name search
3. **Tenant Isolation**: Clients are automatically limited to their tenant. Spark Admin can access all tenants
4. **Validation**: All inputs are validated before processing
5. **Timestamps**: All timestamps are returned in ISO 8601 format
6. **Global IDs**: All IDs are Relay global IDs (base64-encoded type:id format)
7. **Client Mutation ID**: All mutations support `clientMutationId` for tracking purposes (Relay compliance)
8. **Job Requirement**: The `jobId` in `createAmbassadorGroup` must reference a job that exists and has a rate assigned
9. **Members Field**: The `members` field on `AmbassadorGroup` returns all `UserGroup` records linked to the group
10. **Atomic Operations**: When creating an ambassador group with ambassadors, all related records (UserGroup, AmbassadorJob) are created in a single transaction

---

## Role-Based Access

### Clients
- Can access all group type and ambassador group queries and mutations
- Can only access data from their own tenant
- `tenantId` parameter is automatically set to their default tenant

### Spark Admin
- Can access all group type and ambassador group queries and mutations
- Can access data across all tenants
- Can specify `tenantId` in queries and mutations

### Ambassadors
- **Cannot access** group type or ambassador group queries and mutations
- These endpoints require `IsClientOrSparkAdmin` permission

---

## Testing

### Using cURL

```bash
# Set your token
TOKEN="your_jwt_token_here"

# Create Group Type
curl -X POST http://localhost:8000/api/v1/graphql/clients \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "query": "mutation { createGroupType(input: { name: \"Sales Team\" }) { success message groupType { id name } } }"
  }'

# Create Ambassador Group
curl -X POST http://localhost:8000/api/v1/graphql/clients \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "query": "mutation { createAmbassadorGroup(input: { name: \"Sales Team A\" tenantId: \"MTI3\" jobId: \"Sm9iOjE=\" groupTypeId: \"R3JvdXBUeXBlOjE=\" }) { success message ambassadorGroup { id name } } }"
  }'

# Query Ambassador Groups
curl -X POST http://localhost:8000/api/v1/graphql/clients \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "query": "query { ambassadorGroups(first: 10) { edges { node { id name description members { id user { email } } } } totalCount } }"
  }'
```

### Using Python

```python
import requests

url = "http://localhost:8000/api/v1/graphql/clients"
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {token}"
}

# Create Group Type
mutation = """
mutation {
  createGroupType(input: { name: "Sales Team" }) {
    success
    message
    groupType {
      id
      name
    }
  }
}
"""

response = requests.post(url, json={"query": mutation}, headers=headers)
print(response.json())
```

