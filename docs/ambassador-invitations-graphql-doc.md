# Ambassador Invitations GraphQL API Documentation

This document provides comprehensive documentation for the Ambassador Invitations GraphQL API endpoints.

## Table of Contents

- [Base URLs](#base-urls)
- [Authentication](#authentication)
- [Mutations](#mutations)
  - [Accept Invitation by Token](#accept-invitation-by-token)
- [Input Types](#input-types)
- [Response Types](#response-types)
- [Error Handling](#error-handling)
- [Examples](#examples)

---

## Base URLs

The API provides GraphQL endpoints on the following schemas:

- **Spark Admin**: `http://localhost:8000/api/v1/graphql/spark`
- **Clients**: `http://localhost:8000/api/v1/graphql/clients`
- **Ambassadors**: `http://localhost:8000/api/v1/graphql/ambassadors`

---

## Authentication

All endpoints require authentication. You must include a JWT token in the Authorization header:

```
Authorization: Bearer <your_jwt_token>
```

---

## Mutations

### Accept Invitation by Token

Accept an ambassador invitation using a token. This mutation is for authenticated users who want to accept an invitation. If the user doesn't have an ambassador profile, one will be created automatically. If the invitation includes a job, the job invitation will also be accepted.

**Available for**: All authenticated users (Ambassadors, Clients, Spark Admin)

**GraphQL Mutation:**
```graphql
mutation AcceptByToken($input: AcceptByTokenInput!) {
  acceptByToken(input: $input) {
    success
    message
    clientMutationId
    ambassador {
      id
      uuid
      isActive
      address
      coordinates
    }
  }
}
```

**Input Fields:**
- `token` (String, required): The invitation token
- `clientMutationId` (ID, optional): Client mutation ID for tracking

**Response:**
```json
{
  "data": {
    "acceptByToken": {
      "success": true,
      "message": "Invitation accepted successfully.",
      "clientMutationId": "unique-id-123",
      "ambassador": {
        "id": "QW1iYXNzYWRvcjox",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "isActive": false,
        "address": null,
        "coordinates": []
      }
    }
  }
}
```

**Behavior:**
- Validates that the invitation token exists and is usable (not expired, not already used)
- If the user doesn't have an ambassador profile, creates one automatically
- If the user already has an ambassador profile, uses the existing one
- Links the invitation to the ambassador
- If the invitation includes a job, accepts the job invitation (updates AmbassadorJob status to "accepted")
- Marks the invitation as used

**Example:**
```graphql
mutation {
  acceptByToken(input: {
    token: "abc123def456ghi789"
    clientMutationId: "accept-123"
  }) {
    success
    message
    clientMutationId
    ambassador {
      id
      isActive
    }
  }
}
```

**Success Response:**
```json
{
  "data": {
    "acceptByToken": {
      "success": true,
      "message": "Invitation accepted successfully.",
      "clientMutationId": "accept-123",
      "ambassador": {
        "id": "QW1iYXNzYWRvcjox",
        "isActive": false
      }
    }
  }
}
```

**Error Response (Invalid Token):**
```json
{
  "data": {
    "acceptByToken": {
      "success": false,
      "message": "AmbassadorInvitation matching query does not exist.",
      "clientMutationId": "accept-123",
      "ambassador": null
    }
  }
}
```

**Error Response (Expired Invitation):**
```json
{
  "data": {
    "acceptByToken": {
      "success": false,
      "message": "This invitation has expired.",
      "clientMutationId": "accept-123",
      "ambassador": null
    }
  }
}
```

**Error Response (Already Used):**
```json
{
  "data": {
    "acceptByToken": {
      "success": false,
      "message": "This invitation has already been used.",
      "clientMutationId": "accept-123",
      "ambassador": null
    }
  }
}
```

---

## Input Types

### AcceptByTokenInput

Input for accepting an invitation by token.

```graphql
input AcceptByTokenInput {
  token: String!
  clientMutationId: ID
}
```

**Fields:**
- `token` (String, required): The invitation token to accept
- `clientMutationId` (ID, optional): Optional client mutation ID for tracking

---

## Response Types

### AcceptInvitationResponse

Response for invitation acceptance operations.

```graphql
type AcceptInvitationResponse {
  success: Boolean!
  message: String!
  clientMutationId: ID
  ambassador: Ambassador
  activationToken: String
}
```

**Fields:**
- `success` (Boolean, required): Whether the operation was successful
- `message` (String, required): Human-readable message about the result
- `clientMutationId` (ID, optional): The client mutation ID if provided
- `ambassador` (Ambassador, optional): The ambassador object if successful
- `activationToken` (String, optional): Activation token (not used for acceptByToken)

---

## Error Handling

The mutation handles various error cases:

1. **Invalid Token**: Returns `success: false` with message "AmbassadorInvitation matching query does not exist."
2. **Expired Invitation**: Returns `success: false` with message "This invitation has expired."
3. **Already Used**: Returns `success: false` with message "This invitation has already been used."
4. **Other Errors**: Returns `success: false` with a descriptive error message

All errors are returned as part of the response (not as GraphQL errors), allowing the client to handle them gracefully.

---

## Examples

### Basic Usage

```graphql
mutation {
  acceptByToken(input: {
    token: "your-invitation-token-here"
  }) {
    success
    message
    ambassador {
      id
      isActive
    }
  }
}
```

### With Client Mutation ID

```graphql
mutation {
  acceptByToken(input: {
    token: "your-invitation-token-here"
    clientMutationId: "tracking-id-456"
  }) {
    success
    message
    clientMutationId
    ambassador {
      id
      uuid
      isActive
      address
      coordinates
    }
  }
}
```

### Error Handling Example

```graphql
mutation {
  acceptByToken(input: {
    token: "invalid-token"
  }) {
    success
    message
    ambassador {
      id
    }
  }
}
```

**Response:**
```json
{
  "data": {
    "acceptByToken": {
      "success": false,
      "message": "AmbassadorInvitation matching query does not exist.",
      "ambassador": null
    }
  }
}
```

---

## Notes

- The mutation requires the user to be authenticated
- If the user doesn't have an ambassador profile, one will be created automatically
- If the invitation includes a job, the job invitation will be automatically accepted
- The invitation is marked as used after successful acceptance
- Each invitation token can only be used once

