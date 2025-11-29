# Dashboard GraphQL API Documentation

This document provides comprehensive documentation for the Dashboard GraphQL API endpoints. These queries are designed to provide quick data fetch capabilities for client dashboards with aggregated statistics and time series data.

## Table of Contents

- [Base URLs](#base-urls)
- [Authentication](#authentication)
- [Overview](#overview)
- [Queries](#queries)
  - [Events Statistics](#events-statistics)
  - [Events Time Series](#events-time-series)
  - [Ambassadors Statistics](#ambassadors-statistics)
  - [Request Statistics](#request-statistics)
  - [Request Time Series](#request-time-series)
  - [Event Detail](#event-detail)
- [Input Types](#input-types)
- [Response Types](#response-types)
- [Filtering](#filtering)
- [Performance Considerations](#performance-considerations)
- [Examples](#examples)

---

## Base URLs

The Dashboard API is available on the following GraphQL endpoints:

- **Clients**: `http://localhost:8000/api/v1/graphql/clients`
- **Spark Admin**: `http://localhost:8000/api/v1/graphql/spark`

**Note**: Dashboard queries are not available for Ambassadors schema.

---

## Authentication

All dashboard queries require authentication. You must include a JWT token in the Authorization header:

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

## Overview

The Dashboard API provides six main query endpoints designed for dashboard visualizations:

1. **Events Statistics** - Aggregated event statistics with counts by status and location
2. **Events Time Series** - Time series data for events throughout the day (historic)
3. **Ambassadors Statistics** - Statistics about ambassadors working in events
4. **Request Statistics** - Request approval/rejection rates and job assignment statistics
5. **Request Time Series** - Time series data for requests with approval/rejection trends
6. **Event Detail** - Detailed information about a specific event

All queries:
- Require authentication (`StrictIsAuthenticated`)
- Automatically filter by the authenticated user's tenant
- Support optional filters for date ranges, locations, event types, request statuses, etc.
- Are optimized for performance using database-level aggregations

---

## Queries

### Events Statistics

Get aggregated event statistics including total counts, events by status, events by location, and time-based breakdowns.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  eventsStats(
    filters: {
      startDate: "2024-01-01"
      endDate: "2024-12-31"
      locationId: "123"
      eventTypeId: "456"
      eventStatusId: "789"
    }
  ) {
    totalEvents
    eventsToday
    eventsThisWeek
    eventsThisMonth
    eventsByStatus {
      statusId
      statusName
      count
    }
    eventsByLocation {
      locationId
      locationName
      locationCode
      count
    }
  }
}
```

**Parameters:**
- `filters` (DashboardFiltersInput, optional): Filter options (see [Filtering](#filtering))

**Response Fields:**
- `totalEvents` (int): Total number of events matching the filters
- `eventsToday` (int): Number of events created today
- `eventsThisWeek` (int): Number of events created this week
- `eventsThisMonth` (int): Number of events created this month
- `eventsByStatus` (List[EventStatusCount], optional): Events grouped by status
- `eventsByLocation` (List[LocationEventCount], optional): Events grouped by location

---

### Events Time Series

Get time series data for events throughout the day (historic). Supports grouping by hour, day, week, or month.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  eventsTimeSeries(
    filters: {
      startDate: "2024-01-01"
      endDate: "2024-12-31"
      locationCode: "NYC"
    }
    groupBy: DAY
  ) {
    groupBy
    totalCount
    dataPoints {
      timestamp
      count
      value
    }
  }
}
```

**Parameters:**
- `filters` (DashboardFiltersInput, optional): Filter options (see [Filtering](#filtering))
- `groupBy` (TimeGroupBy, optional): Time grouping option (HOUR, DAY, WEEK, MONTH). Default: DAY

**Response Fields:**
- `groupBy` (str): The grouping used (HOUR, DAY, WEEK, MONTH)
- `totalCount` (int): Total number of events in the time series
- `dataPoints` (List[TimeSeriesDataPoint]): Array of time series data points
  - `timestamp` (str): ISO datetime string
  - `count` (int): Number of events at this time point
  - `value` (float, optional): Optional additional metric value

**Time Grouping Options:**
- `HOUR`: Group events by hour (useful for daily views)
- `DAY`: Group events by day (default, useful for weekly/monthly views)
- `WEEK`: Group events by week (useful for monthly/quarterly views)
- `MONTH`: Group events by month (useful for yearly views)

---

### Ambassadors Statistics

Get statistics about ambassadors working in events, including counts by event and location.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  ambassadorsStats(
    filters: {
      startDate: "2024-01-01"
      endDate: "2024-12-31"
      locationId: "123"
    }
  ) {
    totalAmbassadorsWorking
    uniqueAmbassadorsCount
    ambassadorsByEvent {
      eventId
      eventName
      ambassadorCount
    }
    ambassadorsByLocation {
      locationId
      locationName
      locationCode
      ambassadorCount
    }
  }
}
```

**Parameters:**
- `filters` (DashboardFiltersInput, optional): Filter options (see [Filtering](#filtering))

**Response Fields:**
- `totalAmbassadorsWorking` (int): Total number of unique ambassadors working in events
- `uniqueAmbassadorsCount` (int): Distinct ambassadors across all events (same as totalAmbassadorsWorking)
- `ambassadorsByEvent` (List[EventAmbassadorCount], optional): Ambassador counts per event
- `ambassadorsByLocation` (List[LocationAmbassadorCount], optional): Ambassador counts grouped by location

**Note**: Ambassadors are counted from both `AmbassadorEvent` relationships and `AmbassadorJob` relationships to ensure comprehensive coverage.

---

### Request Statistics

Get request statistics including approval/rejection rates and job assignment statistics.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  requestStats(
    filters: {
      startDate: "2024-01-01"
      endDate: "2024-12-31"
      requestStatusId: "123"
      clientId: "456"
    }
  ) {
    totalRequests
    approvedCount
    rejectedCount
    pendingCount
    approvalRate
    rejectionRate
    requestsWithJobsCount
    requestsWithJobsPercentage
    requestsByStatus {
      statusId
      statusName
      count
    }
  }
}
```

**Parameters:**
- `filters` (DashboardFiltersInput, optional): Filter options (see [Filtering](#filtering))

**Response Fields:**
- `totalRequests` (int): Total number of requests matching the filters
- `approvedCount` (int): Number of approved requests (status with `create_event=True`)
- `rejectedCount` (int): Number of rejected requests
- `pendingCount` (int): Number of pending requests (no status assigned)
- `approvalRate` (float): Approval rate as percentage (0-100)
- `rejectionRate` (float): Rejection rate as percentage (0-100)
- `requestsWithJobsCount` (int): Number of requests that have jobs assigned
- `requestsWithJobsPercentage` (float): Percentage of requests with jobs (0-100)
- `requestsByStatus` (List[RequestStatusCount], optional): Requests grouped by status

**Note**: Approval status is determined by the `RequestStatus` with `create_event=True`. If no such status exists, all requests with a status are considered rejected.

---

### Request Time Series

Get time series data for requests with approval/rejection trends and job assignment trends.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  requestTimeSeries(
    filters: {
      startDate: "2024-01-01"
      endDate: "2024-12-31"
    }
    groupBy: WEEK
  ) {
    groupBy
    totalCount
    dataPoints {
      timestamp
      count
    }
    approvalTrend {
      timestamp
      count
    }
    rejectionTrend {
      timestamp
      count
    }
    jobsAssignedTrend {
      timestamp
      count
    }
  }
}
```

**Parameters:**
- `filters` (DashboardFiltersInput, optional): Filter options (see [Filtering](#filtering))
- `groupBy` (TimeGroupBy, optional): Time grouping option (HOUR, DAY, WEEK, MONTH). Default: DAY

**Response Fields:**
- `groupBy` (str): The grouping used (HOUR, DAY, WEEK, MONTH)
- `totalCount` (int): Total number of requests in the time series
- `dataPoints` (List[TimeSeriesDataPoint]): All requests over time
- `approvalTrend` (List[TimeSeriesDataPoint], optional): Approved requests over time
- `rejectionTrend` (List[TimeSeriesDataPoint], optional): Rejected requests over time
- `jobsAssignedTrend` (List[TimeSeriesDataPoint], optional): Requests with jobs assigned over time

---

### Event Detail

Get detailed information about a specific event including related requests, ambassadors, jobs, and statistics.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  eventDetail(
    id: "123"
    filters: {
      locationId: "456"
    }
  ) {
    event {
      id
      uuid
      name
      startTime
      endTime
      address
      eventType {
        id
        name
      }
      status {
        id
        name
      }
    }
    relatedRequestId
    ambassadorsCount
    jobsCount
    location {
      id
      name
      code
    }
    ambassadors {
      ambassadorId
      ambassadorName
      isApproved
      jobsCount
    }
    statistics {
      totalAmbassadors
      approvedAmbassadors
      totalJobs
      activeJobs
      totalRequests
    }
  }
}
```

**Parameters:**
- `id` (ID, required): The ID of the event to retrieve
- `filters` (DashboardFiltersInput, optional): Additional filter options (see [Filtering](#filtering))

**Response Fields:**
- `event` (Event): The event object with all its details
- `relatedRequestId` (ID, optional): ID of the related request if exists
- `ambassadorsCount` (int): Total number of unique ambassadors working in this event
- `jobsCount` (int): Total number of jobs associated with this event
- `location` (Location, optional): Location information from the related request
- `ambassadors` (List[EventAmbassadorInfo], optional): List of ambassadors working in this event
- `statistics` (EventDetailStatistics, optional): Detailed statistics for this event

---

## Input Types

### DashboardFiltersInput

All dashboard queries accept an optional `filters` parameter of type `DashboardFiltersInput`:

```graphql
input DashboardFiltersInput {
  # Tenant filter (optional, uses user's tenant by default)
  tenantId: ID
  
  # Date range filters
  startDate: String  # ISO date string (YYYY-MM-DD)
  endDate: String    # ISO date string (YYYY-MM-DD)
  
  # Location/zone filters
  locationId: ID
  locationCode: String  # Filter by location code (zone)
  
  # Event filters
  eventTypeId: ID
  eventStatusId: ID
  
  # Request filters
  requestStatusId: ID
  # Note: This is used for request-related queries
  requestTypeId: ID
  
  # Additional useful filters
  clientId: ID
  distributorId: ID
  retailerId: ID
}
```

**All filters are optional**. When not provided, queries will return data for the authenticated user's tenant within reasonable defaults (e.g., last 30 days for time series).

### TimeGroupBy

Enum for time series grouping options:

```graphql
enum TimeGroupBy {
  HOUR
  DAY
  WEEK
  MONTH
}
```

---

## Response Types

### EventStats

```graphql
type EventStats {
  totalEvents: Int!
  eventsByStatus: [EventStatusCount!]
  eventsByLocation: [LocationEventCount!]
  eventsToday: Int!
  eventsThisWeek: Int!
  eventsThisMonth: Int!
}
```

### EventTimeSeries

```graphql
type EventTimeSeries {
  dataPoints: [TimeSeriesDataPoint!]!
  groupBy: String!
  totalCount: Int!
}
```

### AmbassadorStats

```graphql
type AmbassadorStats {
  totalAmbassadorsWorking: Int!
  ambassadorsByEvent: [EventAmbassadorCount!]
  ambassadorsByLocation: [LocationAmbassadorCount!]
  uniqueAmbassadorsCount: Int!
}
```

### RequestStats

```graphql
type RequestStats {
  totalRequests: Int!
  approvedCount: Int!
  rejectedCount: Int!
  pendingCount: Int!
  approvalRate: Float!
  rejectionRate: Float!
  requestsWithJobsCount: Int!
  requestsWithJobsPercentage: Float!
  requestsByStatus: [RequestStatusCount!]
}
```

### RequestTimeSeries

```graphql
type RequestTimeSeries {
  dataPoints: [TimeSeriesDataPoint!]!
  groupBy: String!
  totalCount: Int!
  approvalTrend: [TimeSeriesDataPoint!]
  rejectionTrend: [TimeSeriesDataPoint!]
  jobsAssignedTrend: [TimeSeriesDataPoint!]
}
```

### EventDetail

```graphql
type EventDetail {
  event: Event!
  relatedRequestId: ID
  ambassadorsCount: Int!
  jobsCount: Int!
  ambassadors: [EventAmbassadorInfo!]
  location: Location
  statistics: EventDetailStatistics
}
```

### TimeSeriesDataPoint

```graphql
type TimeSeriesDataPoint {
  timestamp: String!  # ISO datetime string
  count: Int!
  value: Float
}
```

---

## Filtering

All dashboard queries support comprehensive filtering through the `DashboardFiltersInput` type. Filters are applied at the database level for optimal performance.

### Date Range Filtering

Use `startDate` and `endDate` to filter by creation date:

```graphql
filters: {
  startDate: "2024-01-01"  # ISO date format (YYYY-MM-DD)
  endDate: "2024-12-31"
}
```

**Default behavior**: If no date range is provided, time series queries default to the last 30 days.

### Location Filtering

Filter by location using either `locationId` or `locationCode`:

```graphql
filters: {
  locationId: "123"  # Filter by location ID
  # OR
  locationCode: "NYC"  # Filter by location code (zone)
}
```

### Event Filtering

Filter events by type or status:

```graphql
filters: {
  eventTypeId: "456"
  eventStatusId: "789"
}
```

### Request Filtering

Filter requests by status, type, or related entities:

```graphql
filters: {
  requestStatusId: "123"
  requestTypeId: "456"
  clientId: "789"
  distributorId: "101"
  retailerId: "112"
}
```

### Combining Filters

All filters can be combined:

```graphql
filters: {
  startDate: "2024-01-01"
  endDate: "2024-12-31"
  locationCode: "NYC"
  eventTypeId: "456"
  clientId: "789"
}
```

---

## Performance Considerations

The Dashboard API is optimized for performance using several techniques:

### Database-Level Aggregations

All statistics are calculated at the database level using Django ORM aggregations (`Count`, `Sum`, `Avg`, etc.), not in Python. This ensures efficient query execution.

### Efficient Joins

Queries use `select_related()` for ForeignKey relationships and `prefetch_related()` for ManyToMany and reverse ForeignKey relationships to minimize database queries.

### Time Series Optimization

Time series queries use database-level date truncation functions (`TruncHour`, `TruncDay`, `TruncWeek`, `TruncMonth`) to group data efficiently.

### Filtering

Filters are applied early in the query chain to reduce the dataset size before aggregations.

### Recommended Practices

1. **Use appropriate date ranges**: Limit date ranges to reasonable periods (e.g., max 1 year for daily grouping, 3 months for hourly grouping)
2. **Combine filters**: Use multiple filters to narrow down results and improve performance
3. **Choose appropriate grouping**: Use `DAY` for most use cases, `HOUR` only for short-term analysis
4. **Cache results**: Consider caching dashboard statistics (5-15 minutes TTL) for frequently accessed data

---

## Examples

### Example 1: Dashboard Overview

Get a complete dashboard overview with all key statistics:

```graphql
query DashboardOverview {
  # Event statistics
  eventsStats: eventsStats {
    totalEvents
    eventsToday
    eventsThisWeek
    eventsThisMonth
  }
  
  # Request statistics
  requests: requestStats {
    totalRequests
    approvalRate
    rejectionRate
    requestsWithJobsPercentage
  }
  
  # Ambassador statistics
  ambassadors: ambassadorsStats {
    totalAmbassadorsWorking
  }
}
```

### Example 2: Event Trends Over Time

Get event trends for the last 3 months grouped by week:

```graphql
query EventTrends {
  eventsTimeSeries(
    filters: {
      startDate: "2024-10-01"
      endDate: "2024-12-31"
    }
    groupBy: WEEK
  ) {
    groupBy
    totalCount
    dataPoints {
      timestamp
      count
    }
  }
}
```

### Example 3: Request Approval Trends

Get request approval and rejection trends:

```graphql
query RequestTrends {
  requestTimeSeries(
    filters: {
      startDate: "2024-01-01"
      endDate: "2024-12-31"
    }
    groupBy: MONTH
  ) {
    dataPoints {
      timestamp
      count
    }
    approvalTrend {
      timestamp
      count
    }
    rejectionTrend {
      timestamp
      count
    }
  }
}
```

### Example 4: Location-Specific Statistics

Get statistics for a specific location:

```graphql
query LocationStats {
  eventsStats(
    filters: {
      locationCode: "NYC"
      startDate: "2024-01-01"
      endDate: "2024-12-31"
    }
  ) {
    totalEvents
    eventsByStatus {
      statusName
      count
    }
  }
  
  ambassadorsStats(
    filters: {
      locationCode: "NYC"
    }
  ) {
    totalAmbassadorsWorking
    ambassadorsByEvent {
      eventName
      ambassadorCount
    }
  }
}
```

### Example 5: Event Detail View

Get detailed information about a specific event:

```graphql
query EventDetails {
  eventDetail(id: "123") {
    event {
      id
      name
      startTime
      endTime
      address
      eventType {
        name
      }
      status {
        name
      }
    }
    ambassadorsCount
    jobsCount
    location {
      name
      code
    }
    ambassadors {
      ambassadorName
      isApproved
      jobsCount
    }
    statistics {
      totalAmbassadors
      approvedAmbassadors
      totalJobs
      activeJobs
    }
  }
}
```

### Example 6: Client-Specific Dashboard

Get dashboard data filtered by client:

```graphql
query ClientDashboard {
  eventsStats(
    filters: {
      clientId: "456"
      startDate: "2024-01-01"
      endDate: "2024-12-31"
    }
  ) {
    totalEvents
    eventsThisMonth
  }
  
  requestStats(
    filters: {
      clientId: "456"
    }
  ) {
    totalRequests
    approvalRate
    requestsWithJobsCount
  }
}
```

---

## Error Handling

All queries will return errors in the standard GraphQL error format if:

- Authentication fails (missing or invalid JWT token)
- User doesn't have access to the requested tenant
- Invalid filter values are provided
- Event not found (for `eventDetail` query)

Example error response:

```json
{
  "errors": [
    {
      "message": "You must be authenticated to perform this action.",
      "extensions": {
        "code": "UNAUTHENTICATED"
      }
    }
  ]
}
```

---

## Notes

- All queries automatically filter by the authenticated user's tenant unless `tenantId` is explicitly provided in filters (Spark Admin only)
- Date filters use ISO date format (YYYY-MM-DD)
- Time series data points are ordered chronologically
- Percentages are returned as floats (0-100), not decimals (0-1)
- Empty lists are returned as `null` rather than empty arrays for optional list fields
- All timestamps are in ISO 8601 format

