# Jobs GraphQL API Documentation

This document provides comprehensive documentation for the Jobs GraphQL API endpoints.

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

All queries use **Relay-style pagination** with the following parameters:

- `first` (int, optional): Number of items to return from the start
- `after` (string, optional): Cursor for pagination (from previous query)
- `last` (int, optional): Number of items to return from the end
- `before` (string, optional): Cursor for pagination (from previous query)
- `q` (string, optional): Search query to filter by name (case-insensitive)

All queries return a `CountableConnection` type with the following structure:

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

### Available Jobs (Ambassadors Only)

Get all available jobs for ambassadors. Returns jobs that are ongoing, not closed, and public. Includes prefetched `jobRequirements`.

**Available for**: Ambassadors only

**GraphQL Query:**
```graphql
query {
  availableJobs(
    first: 10
    after: null
    q: "developer"
  ) {
    edges {
      node {
        id
        uuid
        name
        description
        code
        address
        startDate
        endDate
        public
        closed
        national
        ongoing
        jobRequirements {
          id
          name
          jobRequirementType {
            id
            name
          }
        }
        jobTitle {
          id
          name
        }
        company {
          id
          name
          email
        }
        rate {
          id
          amout
          rateType {
            id
            name
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

**Note**: The `jobRequirements` field is only available when prefetched (as in this query). If not prefetched, it will return `null`.

---

### Ambassador Job Statuses

Get all ambassador job statuses for the authenticated user's tenant.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Query:**
```graphql
query {
  ambassadorJobStatuses(
    first: 10
    after: null
    q: "active"
  ) {
    edges {
      node {
        id
        uuid
        name
        tenantId
        createdAt
        updatedAt
      }
      cursor
    }
    pageInfo {
      hasNextPage
      hasPreviousPage
    }
    totalCount
  }
}
```

**Single Record Query:**
```graphql
query {
  ambassadorJobStatus(id: "1") {
    id
    uuid
    name
    tenantId
    createdAt
    updatedAt
  }
}
```

---

### Companies

Get all companies for the authenticated user's tenant.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Query:**
```graphql
query {
  companies(
    first: 10
    after: null
    q: "tech"
  ) {
    edges {
      node {
        id
        uuid
        name
        email
        websiteUrl
        foundingDate
        phone
        address
        aboutUs
        companySizeMin
        companySizeMax
        approved
        location {
          id
          name
          code
        }
        cover {
          id
          name
          url
        }
        profileImage {
          id
          name
          url
        }
        createdAt
        updatedAt
      }
      cursor
    }
    pageInfo {
      hasNextPage
      hasPreviousPage
    }
    totalCount
  }
}
```

**Single Record Query:**
```graphql
query {
  company(id: "1") {
    id
    uuid
    name
    email
    websiteUrl
    phone
    address
    aboutUs
    approved
    location {
      id
      name
    }
    createdAt
    updatedAt
  }
}
```

---

### Jobs

Get all jobs for the authenticated user's tenant.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Query:**
```graphql
query {
  jobs(
    first: 10
    after: null
    q: "developer"
  ) {
    edges {
      node {
        id
        uuid
        name
        description
        code
        address
        startDate
        endDate
        public
        closed
        national
        ongoing
        jobTitle {
          id
          name
        }
        company {
          id
          name
        }
        event {
          id
          name
        }
        location {
          id
          name
        }
        rate {
          id
          amout
        }
        createdAt
        updatedAt
      }
      cursor
    }
    pageInfo {
      hasNextPage
      hasPreviousPage
    }
    totalCount
  }
}
```

**Single Record Query:**
```graphql
query {
  job(id: "1") {
    id
    uuid
    name
    description
    code
    address
    startDate
    endDate
    public
    closed
    national
    ongoing
    jobTitle {
      id
      name
    }
    company {
      id
      name
    }
    rate {
      id
      amout
    }
    createdAt
    updatedAt
  }
}
```

---

### Ambassador Jobs

Get all ambassador jobs for the authenticated user's tenant.

**Available for**: Ambassadors only

**GraphQL Query:**
```graphql
query {
  ambassadorJobs(
    first: 10
    after: null
    q: null
  ) {
    edges {
      node {
        id
        uuid
        job {
          id
          name
          description
        }
        ambassador {
          id
          email
        }
        status {
          id
          name
        }
        createdAt
        updatedAt
      }
      cursor
    }
    pageInfo {
      hasNextPage
      hasPreviousPage
    }
    totalCount
  }
}
```

**Single Record Query:**
```graphql
query {
  ambassadorJob(id: "1") {
    id
    uuid
    job {
      id
      name
    }
    ambassador {
      id
      email
    }
    status {
      id
      name
    }
    createdAt
    updatedAt
  }
}
```

---

## Mutations

All mutations follow a consistent pattern and use Relay-style input with `clientMutationId` support. All mutations require authentication.

### Create Ambassador Job Status

Create a new ambassador job status.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createAmbassadorJobStatus(input: {
    name: "Active"
    tenantId: "1"
    clientMutationId: "unique-id-123"
  }) {
    success
    message
    clientMutationId
    status {
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
- `name` (string, required): Status name
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)
- `clientMutationId` (ID, optional): Client mutation ID for tracking

**Response:**
```json
{
  "data": {
    "createAmbassadorJobStatus": {
      "success": true,
      "message": "Status created successfully.",
      "clientMutationId": "unique-id-123",
      "status": {
        "id": "1",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "name": "Active",
        "tenantId": "1",
        "createdAt": "2025-01-15T10:00:00Z",
        "updatedAt": "2025-01-15T10:00:00Z"
      }
    }
  }
}
```

---

### Update Ambassador Job Status

Update an existing ambassador job status.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateAmbassadorJobStatus(input: {
    id: "1"
    name: "Updated Status Name"
    tenantId: "1"
    clientMutationId: "unique-id-456"
  }) {
    success
    message
    clientMutationId
    status {
      id
      uuid
      name
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the status to update
- `name` (string, required): Updated status name
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)
- `clientMutationId` (ID, optional): Client mutation ID for tracking

---

### Create Company

Create a new company.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createCompany(input: {
    name: "Acme Corporation"
    email: "contact@acme.com"
    phone: "+1-555-123-4567"
    websiteUrl: "https://acme.com"
    address: "123 Main St, New York, NY 10001"
    aboutUs: "Leading technology company"
    companySizeMin: 50
    companySizeMax: 200
    approved: false
    locationId: "1"
    coverId: "2"
    profileImageId: "3"
    tenantId: "1"
    clientMutationId: "unique-id-789"
  }) {
    success
    message
    clientMutationId
    company {
      id
      uuid
      name
      email
      phone
      websiteUrl
      address
      aboutUs
      companySizeMin
      companySizeMax
      approved
      location {
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
- `name` (string, required): Company name
- `email` (string, required): Company email address
- `phone` (string, required): Company phone number
- `websiteUrl` (string, optional): Company website URL
- `foundingDate` (string, optional): Company founding date (ISO 8601)
- `address` (string, optional): Company address
- `aboutUs` (string, optional): Company description
- `companySizeMin` (int, optional): Minimum company size
- `companySizeMax` (int, optional): Maximum company size
- `approved` (bool, optional): Whether the company is approved (default: false)
- `locationId` (ID, optional): ID of the associated location
- `coverId` (ID, optional): ID of the cover image file
- `profileImageId` (ID, optional): ID of the profile image file
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)
- `clientMutationId` (ID, optional): Client mutation ID for tracking

---

### Update Company

Update an existing company.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateCompany(input: {
    id: "1"
    name: "Updated Company Name"
    email: "newemail@acme.com"
    phone: "+1-555-987-6543"
    websiteUrl: "https://newacme.com"
    approved: true
    tenantId: "1"
    clientMutationId: "unique-id-101"
  }) {
    success
    message
    clientMutationId
    company {
      id
      uuid
      name
      email
      phone
      websiteUrl
      approved
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the company to update
- All fields from `CreateCompanyInput` are optional for updates
- `clientMutationId` (ID, optional): Client mutation ID for tracking

---

### Create Job

Create a new job.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  createJob(input: {
    name: "Senior Software Engineer"
    description: "We are looking for an experienced software engineer"
    code: "JOB-001"
    address: "123 Tech St, San Francisco, CA 94105"
    startDate: "2025-02-01"
    endDate: "2025-12-31"
    public: true
    closed: false
    national: false
    ongoing: true
    jobTitleId: "1"
    otherTitleId: "2"
    companyId: "1"
    eventId: "1"
    locationId: "1"
    rateId: "1"
    tenantId: "1"
    clientMutationId: "unique-id-202"
  }) {
    success
    message
    clientMutationId
    job {
      id
      uuid
      name
      description
      code
      address
      startDate
      endDate
      public
      closed
      national
      ongoing
      jobTitle {
        id
        name
      }
      company {
        id
        name
      }
      rate {
        id
        amout
      }
      createdAt
      updatedAt
    }
  }
}
```

**Input Fields:**
- `name` (string, required): Job name
- `description` (string, optional): Job description
- `code` (string, required): Job code
- `address` (string, required): Job address
- `startDate` (string, optional): Job start date (ISO 8601)
- `endDate` (string, optional): Job end date (ISO 8601)
- `public` (bool, optional): Whether the job is public (default: false)
- `closed` (bool, optional): Whether the job is closed (default: false)
- `national` (bool, optional): Whether the job is national (default: false)
- `ongoing` (bool, optional): Whether the job is ongoing (default: false)
- `jobTitleId` (ID, required): ID of the job title
- `otherTitleId` (ID, optional): ID of the other job title
- `companyId` (ID, required): ID of the company
- `eventId` (ID, required): ID of the associated event
- `locationId` (ID, required): ID of the location
- `rateId` (ID, required): ID of the rate
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)
- `clientMutationId` (ID, optional): Client mutation ID for tracking

---

### Update Job

Update an existing job.

**Available for**: Ambassadors, Spark Admin, Clients

**GraphQL Mutation:**
```graphql
mutation {
  updateJob(input: {
    id: "1"
    name: "Updated Job Title"
    description: "Updated job description"
    closed: true
    tenantId: "1"
    clientMutationId: "unique-id-303"
  }) {
    success
    message
    clientMutationId
    job {
      id
      uuid
      name
      description
      closed
      updatedAt
    }
  }
}
```

**Input Fields:**
- `id` (ID, required): The ID of the job to update
- All fields from `CreateJobInput` are optional for updates
- `clientMutationId` (ID, optional): Client mutation ID for tracking

---

## Complete List of Available Queries and Mutations

The Jobs API provides queries and mutations for the following models:

### Queries

Each model has two queries:
1. **Connection query** (plural, e.g., `companies`): Returns a paginated list
2. **Single record query** (singular, e.g., `company`): Returns a single record by ID

**Available Query Sets:**

1. **Status**: `ambassadorJobStatuses`, `ambassadorJobStatus`
2. **CompanyFile**: `companyFiles`, `companyFile`
3. **Company**: `companies`, `company`
4. **CompanyReview**: `companyReviews`, `companyReview`
5. **PayTiming**: `payTimings`, `payTiming`
6. **ReviewScore**: `reviewScores`, `reviewScore`
7. **JobTitle**: `jobTitles`, `jobTitle`
8. **RateType**: `rateTypes`, `rateType`
9. **Rate**: `rates`, `rate`
10. **Job**: `jobs`, `job`
11. **JobFile**: `jobFiles`, `jobFile`
12. **JobRequirementType**: `jobRequirementTypes`, `jobRequirementType`
13. **JobRequirement**: `jobRequirements`, `jobRequirement`
14. **JobRequirementFile**: `jobRequirementFiles`, `jobRequirementFile`
15. **AmbassadorJob**: `ambassadorJobs`, `ambassadorJob` (Ambassadors only)
16. **CompanyToAmbassadorReview**: `companyToAmbassadorReviews`, `companyToAmbassadorReview`
17. **AmbassadorToAmbassadorReview**: `ambassadorToAmbassadorReviews`, `ambassadorToAmbassadorReview` (Ambassadors only)
18. **QuestionType**: `questionTypes`, `questionType`
19. **JobRequirementQuestion**: `jobRequirementQuestions`, `jobRequirementQuestion`
20. **QuestionOption**: `questionOptions`, `questionOption`
21. **JobRequirementAnswer**: `jobRequirementAnswers`, `jobRequirementAnswer`

**Special Query:**
- **Available Jobs**: `availableJobs` (Ambassadors only) - Returns available jobs with prefetched `jobRequirements`

### Mutations

Each model has two mutations:
1. **Create mutation** (e.g., `createCompany`): Creates a new record
2. **Update mutation** (e.g., `updateCompany`): Updates an existing record

**Available Mutation Sets:**

1. **Status**: `createAmbassadorJobStatus`, `updateAmbassadorJobStatus`
2. **CompanyFile**: `createCompanyFile`, `updateCompanyFile`
3. **Company**: `createCompany`, `updateCompany`
4. **CompanyReview**: `createCompanyReview`, `updateCompanyReview`
5. **PayTiming**: `createPayTiming`, `updatePayTiming`
6. **ReviewScore**: `createReviewScore`, `updateReviewScore`
7. **JobTitle**: `createJobTitle`, `updateJobTitle`
8. **RateType**: `createRateType`, `updateRateType`
9. **Rate**: `createRate`, `updateRate`
10. **Job**: `createJob`, `updateJob`
11. **JobFile**: `createJobFile`, `updateJobFile`
12. **JobRequirementType**: `createJobRequirementType`, `updateJobRequirementType`
13. **JobRequirement**: `createJobRequirement`, `updateJobRequirement`
14. **JobRequirementFile**: `createJobRequirementFile`, `updateJobRequirementFile`
15. **AmbassadorJob**: `createAmbassadorJob`, `updateAmbassadorJob` (Ambassadors only)
16. **CompanyToAmbassadorReview**: `createCompanyToAmbassadorReview`, `updateCompanyToAmbassadorReview`
17. **AmbassadorToAmbassadorReview**: `createAmbassadorToAmbassadorReview`, `updateAmbassadorToAmbassadorReview` (Ambassadors only)
18. **QuestionType**: `createQuestionType`, `updateQuestionType`
19. **JobRequirementQuestion**: `createJobRequirementQuestion`, `updateJobRequirementQuestion`
20. **QuestionOption**: `createQuestionOption`, `updateQuestionOption`
21. **JobRequirementAnswer**: `createJobRequirementAnswer`, `updateJobRequirementAnswer`

---

## Types

### Status

Represents an ambassador job status.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Status name
- `tenantId` (ID): Tenant ID
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### Company

Represents a company in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Company name
- `email` (string): Company email address
- `websiteUrl` (string, nullable): Company website URL
- `foundingDate` (string, nullable): Company founding date (ISO 8601)
- `phone` (string): Company phone number
- `address` (string, nullable): Company address
- `aboutUs` (string, nullable): Company description
- `companySizeMin` (int, nullable): Minimum company size
- `companySizeMax` (int, nullable): Maximum company size
- `approved` (bool): Whether the company is approved
- `tenantId` (ID, nullable): Tenant ID
- `location` (Location, nullable): Associated location
- `cover` (CompanyFile, nullable): Cover image file
- `profileImage` (CompanyFile, nullable): Profile image file
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### Job

Represents a job in the system.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `name` (string): Job name
- `description` (string, nullable): Job description
- `code` (string): Job code
- `address` (string): Job address
- `startDate` (string, nullable): Job start date (ISO 8601)
- `endDate` (string, nullable): Job end date (ISO 8601)
- `public` (bool): Whether the job is public
- `closed` (bool): Whether the job is closed
- `national` (bool): Whether the job is national
- `ongoing` (bool): Whether the job is ongoing
- `jobTitle` (JobTitle): Associated job title
- `otherTitle` (JobTitle, nullable): Other job title
- `company` (Company): Associated company
- `event` (Event): Associated event
- `location` (Location): Associated location
- `tenantId` (ID): Tenant ID
- `rate` (Rate): Associated rate
- `jobRequirements` (List[JobRequirement], nullable): Job requirements (only available when prefetched)
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

### AmbassadorJob

Represents an ambassador's job assignment.

**Fields:**
- `id` (ID): Unique identifier
- `uuid` (string): UUID identifier
- `job` (Job): Associated job
- `ambassador` (Ambassador): Associated ambassador
- `status` (Status): Current status
- `createdAt` (string): Creation timestamp (ISO 8601)
- `updatedAt` (string): Last update timestamp (ISO 8601)

---

## Input Types

All input types inherit from `SparkGraphQLInput` which includes:
- `clientMutationId` (ID, optional): Client mutation ID for tracking

### CreateStatusInput

**Fields:**
- `name` (string, required): Status name
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)
- `clientMutationId` (ID, optional): Client mutation ID

---

### CreateCompanyInput

**Fields:**
- `name` (string, required): Company name
- `email` (string, required): Company email address
- `phone` (string, required): Company phone number
- `websiteUrl` (string, optional): Company website URL
- `foundingDate` (string, optional): Company founding date (ISO 8601)
- `address` (string, optional): Company address
- `aboutUs` (string, optional): Company description
- `companySizeMin` (int, optional): Minimum company size
- `companySizeMax` (int, optional): Maximum company size
- `approved` (bool, optional): Whether the company is approved (default: false)
- `locationId` (ID, optional): ID of the associated location
- `coverId` (ID, optional): ID of the cover image file
- `profileImageId` (ID, optional): ID of the profile image file
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)
- `clientMutationId` (ID, optional): Client mutation ID

---

### UpdateCompanyInput

Extends `CreateCompanyInput` with:
- `id` (ID, required): Company ID to update

---

### CreateJobInput

**Fields:**
- `name` (string, required): Job name
- `description` (string, optional): Job description
- `code` (string, required): Job code
- `address` (string, required): Job address
- `startDate` (string, optional): Job start date (ISO 8601)
- `endDate` (string, optional): Job end date (ISO 8601)
- `public` (bool, optional): Whether the job is public (default: false)
- `closed` (bool, optional): Whether the job is closed (default: false)
- `national` (bool, optional): Whether the job is national (default: false)
- `ongoing` (bool, optional): Whether the job is ongoing (default: false)
- `jobTitleId` (ID, required): ID of the job title
- `otherTitleId` (ID, optional): ID of the other job title
- `companyId` (ID, required): ID of the company
- `eventId` (ID, required): ID of the associated event
- `locationId` (ID, required): ID of the location
- `rateId` (ID, required): ID of the rate
- `tenantId` (ID, optional): Tenant ID (only Spark Admin can provide this)
- `clientMutationId` (ID, optional): Client mutation ID

---

### UpdateJobInput

Extends `CreateJobInput` with:
- `id` (ID, required): Job ID to update

---

## Response Types

All mutation responses follow a consistent pattern:

### StatusDetailResponse

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `clientMutationId` (ID, nullable): Client mutation ID (echoed from input)
- `status` (Status, nullable): The created/updated status (null on error)

---

### CompanyDetailResponse

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `clientMutationId` (ID, nullable): Client mutation ID (echoed from input)
- `company` (Company, nullable): The created/updated company (null on error)

---

### JobDetailResponse

**Fields:**
- `success` (boolean): Whether the operation was successful
- `message` (string): Response message
- `clientMutationId` (ID, nullable): Client mutation ID (echoed from input)
- `job` (Job, nullable): The created/updated job (null on error)

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

#### Step 2: Create Company
```graphql
mutation {
  createCompany(input: {
    name: "Tech Corp"
    email: "contact@techcorp.com"
    phone: "+1-555-123-4567"
    address: "123 Tech St, San Francisco, CA"
    approved: false
  }) {
    success
    message
    company {
      id
      name
      email
    }
  }
}
```

#### Step 3: Create Job Title
```graphql
mutation {
  createJobTitle(input: {
    name: "Software Engineer"
  }) {
    success
    message
    jobTitle {
      id
      name
    }
  }
}
```

#### Step 4: Create Rate Type
```graphql
mutation {
  createRateType(input: {
    name: "Hourly"
  }) {
    success
    message
    rateType {
      id
      name
    }
  }
}
```

#### Step 5: Create Rate
```graphql
mutation {
  createRate(input: {
    amout: 50.00
    rateTypeId: "1"
  }) {
    success
    message
    rate {
      id
      amout
      rateType {
        id
        name
      }
    }
  }
}
```

#### Step 6: Create Job
```graphql
mutation {
  createJob(input: {
    name: "Senior Software Engineer"
    description: "We are looking for an experienced software engineer"
    code: "JOB-001"
    address: "123 Tech St, San Francisco, CA 94105"
    startDate: "2025-02-01"
    endDate: "2025-12-31"
    public: true
    ongoing: true
    jobTitleId: "1"
    companyId: "1"
    eventId: "1"
    locationId: "1"
    rateId: "1"
  }) {
    success
    message
    job {
      id
      name
      description
      code
      company {
        id
        name
      }
      jobTitle {
        id
        name
      }
      rate {
        id
        amout
      }
    }
  }
}
```

#### Step 7: Query Available Jobs (Ambassadors)
```graphql
query {
  availableJobs(first: 10) {
    edges {
      node {
        id
        name
        description
        company {
          id
          name
        }
        jobRequirements {
          id
          name
        }
      }
    }
    pageInfo {
      hasNextPage
    }
    totalCount
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
     "data": {
       "createCompany": {
         "success": false,
         "message": "Validation errors: Name is required., Email is required.",
         "company": null
       }
     }
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
     "data": {
       "updateJob": {
         "success": false,
         "message": "Record not found.",
         "job": null
       }
     }
   }
   ```

4. **Tenant Access Error**
   ```json
   {
     "data": {
       "createCompany": {
         "success": false,
         "message": "You don't have access to this tenant",
         "company": null
       }
     }
   }
   ```

---

## Role-Based Access

### Ambassadors
- Can access ambassador-specific queries (`availableJobs`, `ambassadorJobs`, `ambassadorToAmbassadorReviews`)
- Can only access data from their own tenant
- `tenantId` parameter is automatically set to their default tenant
- Cannot specify `tenantId` in mutations (will raise error)

### Clients
- Can access all client-available queries and mutations
- Can only access data from their own tenant
- `tenantId` parameter is automatically set to their default tenant
- Cannot specify `tenantId` in mutations (will raise error)

### Spark Admin
- Can access all queries and mutations across all tenants
- Can specify `tenantId` in queries and mutations
- Has full CRUD access across all tenants

---

## Notes

1. **Relay Pagination**: All list queries use Relay-style pagination with `first`, `after`, `last`, `before` parameters
2. **Search**: Use the `q` parameter for case-insensitive name search
3. **Tenant Isolation**: Ambassadors and Clients are automatically limited to their tenant
4. **Validation**: All inputs are validated before processing
5. **Timestamps**: All timestamps are returned in ISO 8601 format
6. **Prefetching**: The `jobRequirements` field on `Job` is only available when prefetched (as in `availableJobs` query)
7. **Client Mutation ID**: All mutations support `clientMutationId` for tracking purposes (Relay compliance)

---

## Testing

### Using cURL

```bash
# Set your token
TOKEN="your_jwt_token_here"

# Create Company
curl -X POST http://localhost:8000/api/v1/graphql/clients \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "query": "mutation { createCompany(input: { name: \"Test Company\", email: \"test@example.com\", phone: \"+1-555-123-4567\" }) { success message company { id name } } }"
  }'
```

### Using Python

```python
import requests

url = "http://localhost:8000/api/v1/graphql/clients"
headers = {
    "Authorization": "Bearer your_token_here",
    "Content-Type": "application/json"
}

mutation = """
mutation {
  createCompany(input: {
    name: "Test Company"
    email: "test@example.com"
    phone: "+1-555-123-4567"
  }) {
    success
    message
    company {
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

