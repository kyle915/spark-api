# Dashboard GraphQL API Documentation

This document provides comprehensive documentation for the Event Dashboard and Recap Dashboard GraphQL API endpoints. Both dashboards are designed for admin users to get comprehensive insights and analytics about events, recaps, consumer engagements, brand awareness, purchase intent, sales performance, and market analysis.

## Table of Contents

- [Base URLs](#base-urls)
- [Authentication](#authentication)
- [Overview](#overview)
- [Queries](#queries)
  - [Event Dashboard](#event-dashboard-queries)
    - [Event Dashboard Filter Options](#event-dashboard-filter-options)
    - [Event Dashboard](#event-dashboard)
  - [Recap Dashboard](#recap-dashboard-queries)
    - [Recap Dashboard Filter Options](#recap-dashboard-filter-options)
    - [Recap Dashboard](#recap-dashboard)
- [Input Types](#input-types)
- [Response Types](#response-types)
- [Filtering](#filtering)
- [Performance Considerations](#performance-considerations)
- [Examples](#examples)

---

## Base URLs

The Event Dashboard API is available on the following GraphQL endpoints:

- **Clients**: `http://localhost:8000/api/v1/graphql/clients`
- **Spark Admin**: `http://localhost:8000/api/v1/graphql/spark`

**Note**: Both Event Dashboard and Recap Dashboard queries are available for authenticated admin users who need to overview all data across tenants.

---

## Authentication

All Dashboard queries require authentication. You must include a JWT token in the Authorization header:

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

The Dashboard API provides four main query endpoints designed for comprehensive analytics:

### Event Dashboard

1. **Event Dashboard Filter Options** - Get available filter options (distributors, retailers/RMMs, quarters, tenants)
2. **Event Dashboard** - Get comprehensive dashboard data including metrics, trends, insights, and recent events

### Recap Dashboard

3. **Recap Dashboard Filter Options** - Get available filter options (distributors, retailers/RMMs, quarters, tenants)
4. **Recap Dashboard** - Get comprehensive dashboard data including metrics, trends, insights, market analysis, and RMM performance

### Key Features

Both dashboards share these features:

- **Admin Dashboard**: Shows all data across all tenants by default (admin view)
- **Tenant Filtering**: Optionally filter by tenant when provided in filters
- **Quarter-Based Analysis**: Defaults to current quarter, supports quarter and date range filtering
- **Consumer Engagement Metrics**: Tracks consumers sampled, brand awareness, and purchase intent
- **Performance Insights**: Provides period-over-period comparisons and growth rates
- **Monthly Trends**: Shows monthly performance trends with conversion rates

**Event Dashboard Specific:**
- **Recent Events**: Displays upcoming events with consumer engagement data

**Recap Dashboard Specific:**
- **Sales Metrics**: Tracks purchases, revenue, and conversion rates
- **Market Analysis**: Groups performance by retailer/market with efficiency metrics
- **RMM Performance**: Shows retailer performance with consumers, demos, and conversion rates

All queries:
- Require authentication (`StrictIsAuthenticated`)
- Show all data by default (admin dashboard)
- Support optional filters for tenant, quarter, date range, distributor, and retailer/RMM
- Are optimized for performance using database-level aggregations and caching

---

## Queries

## Event Dashboard Queries

### Event Dashboard Filter Options

Get available filter options for the Event Dashboard. This query returns all available distributors, retailers (RMMs), quarters, and tenants that can be used to filter the Event Dashboard data.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  eventDashboardFilterOptions {
    distributors {
      id
      name
    }
    rmms {
      id
      name
      address
    }
    quarters {
      value
      label
    }
    tenants {
      id
      name
    }
  }
}
```

**Parameters:**
- None (no parameters required)

**Response Fields:**
- `distributors` (List[DistributorOption], optional): Available distributors from events with recaps
  - `id` (ID): Distributor ID
  - `name` (str): Distributor name
- `rmms` (List[RetailerOption], optional): Available retailers/RMMs from events with recaps
  - `id` (ID): Retailer ID
  - `name` (str): Retailer name
  - `address` (str): Retailer address
- `quarters` (List[QuarterOption], optional): Available quarters (last 2 years + current year)
  - `value` (str): Quarter string (e.g., "Q1 2025")
  - `label` (str): Quarter label (same as value)
- `tenants` (List[TenantOption], optional): Tenants the authenticated user has access to
  - `id` (ID): Tenant ID
  - `name` (str): Tenant name

**Note**: This query is cached for 1 hour as filter options rarely change.

---

### Event Dashboard

Get comprehensive Event Dashboard data including key metrics, monthly trends, performance insights, and recent events.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  eventDashboard(
    filters: {
      tenantId: "123"
      quarter: "Q1 2025"
      distributorId: "456"
      rmmId: "789"
    }
  ) {
    metrics {
      totalEvents
      consumersSampled
      brandAwareness
      purchaseIntent
      comparisonPeriod
      comparisonValues {
        totalEvents
        consumersSampled
        brandAwareness
        purchaseIntent
      }
    }
    monthlyTrends {
      dataPoints {
        month
        consumersSampled
        willingToPurchase
        conversionRate
        eventsCount
      }
    }
    performanceInsights {
      knewAboutBrand
      knewAboutBrandPercentage
      willingToPurchase
      willingToPurchasePercentage
      bestMonth {
        month
        eventsCount
        consumersCount
      }
      growthRate
    }
    recentEvents {
      id
      name
      date
      location
      consumers
      intentRate
      status
    }
  }
}
```

**Parameters:**
- `filters` (EventDashboardFiltersInput, optional): Filter options (see [Filtering](#filtering))

**Response Fields:**

#### Metrics (EventDashboardMetrics)
- `totalEvents` (int): Total number of events in the selected period
- `consumersSampled` (int): Total consumers sampled (sum of `total_consumer` from ConsumerEngagements)
- `brandAwareness` (float): Brand awareness percentage (0-100) - percentage of consumers who knew about the brand
- `purchaseIntent` (float): Purchase intent percentage (0-100) - percentage of consumers willing to purchase
- `comparisonPeriod` (str, optional): Comparison period string (e.g., "Q4 2024") - shown when quarter filter is used
- `comparisonValues` (ComparisonValues, optional): Previous period comparison values
  - `totalEvents` (int): Events in comparison period
  - `consumersSampled` (int): Consumers sampled in comparison period
  - `brandAwareness` (float): Brand awareness in comparison period
  - `purchaseIntent` (float): Purchase intent in comparison period

#### Monthly Trends (MonthlyPerformanceTrend)
- `dataPoints` (List[MonthlyDataPoint]): Monthly performance data points
  - `month` (str): Month string (e.g., "2025-01")
  - `consumersSampled` (int): Total consumers sampled in this month
  - `willingToPurchase` (int): Consumers willing to purchase in this month
  - `conversionRate` (float): Conversion rate percentage (0-100) - willing to purchase / total consumers
  - `eventsCount` (int): Number of events in this month

#### Performance Insights (PerformanceInsights)
- `knewAboutBrand` (int): Total consumers who knew about the brand
- `knewAboutBrandPercentage` (float): Percentage of consumers who knew about the brand (0-100)
- `willingToPurchase` (int): Total consumers willing to purchase
- `willingToPurchasePercentage` (float): Percentage of consumers willing to purchase (0-100)
- `bestMonth` (BestMonth, optional): Best performing month based on consumers sampled
  - `month` (str): Month string (e.g., "2025-06")
  - `eventsCount` (int): Number of events in best month
  - `consumersCount` (int): Consumers sampled in best month
- `growthRate` (float): Growth rate percentage - events growth vs same quarter last year (can be negative)

#### Recent Events (List[RecentEvent], optional)
- `id` (ID): Event ID
- `name` (str): Event name
- `date` (str): Event date (ISO date string)
- `location` (str): Retailer/RMM name
- `consumers` (int): Total consumers for this event
- `intentRate` (float): Purchase intent rate percentage (0-100)
- `status` (str): Event status (e.g., "Upcoming", "Completed")

**Default Behavior:**
- If no filters are provided, defaults to **current quarter** for all tenants
- Shows **all data across all tenants** by default (admin dashboard)
- Only filters by tenant if `tenantId` is explicitly provided in filters

---

## Recap Dashboard Queries

### Recap Dashboard Filter Options

Get available filter options for the Recap Dashboard. This query returns all available distributors, retailers (RMMs), quarters, and tenants that can be used to filter the Recap Dashboard data.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  recapDashboardFilterOptions {
    distributors {
      id
      name
    }
    rmms {
      id
      name
      address
    }
    quarters {
      value
      label
    }
    tenants {
      id
      name
    }
  }
}
```

**Parameters:**
- None (no parameters required)

**Response Fields:**
- `distributors` (List[DistributorOption], optional): Available distributors from recaps
  - `id` (ID): Distributor ID
  - `name` (str): Distributor name
- `rmms` (List[RetailerOption], optional): Available retailers/RMMs from recaps
  - `id` (ID): Retailer ID
  - `name` (str): Retailer name
  - `address` (str): Retailer address
- `quarters` (List[QuarterOption], optional): Available quarters (last 2 years + current year)
  - `value` (str): Quarter string (e.g., "Q1 2025")
  - `label` (str): Quarter label (same as value)
- `tenants` (List[TenantOption], optional): Tenants the authenticated user has access to
  - `id` (ID): Tenant ID
  - `name` (str): Tenant name

**Note**: This query is cached for 1 hour as filter options rarely change.

---

### Recap Dashboard

Get comprehensive Recap Dashboard data including key metrics, monthly trends, performance insights, market analysis, and RMM performance.

**Available for**: Clients, Spark Admin

**GraphQL Query:**
```graphql
query {
  recapDashboard(
    filters: {
      tenantId: "123"
      quarter: "Q1 2025"
      distributorId: "456"
      rmmId: "789"
    }
  ) {
    metrics {
      totalConsumersSampled
      totalPurchases
      conversionRate
      revenueGenerated
      comparisonPeriod
      comparisonValues {
        totalConsumersSampled
        totalPurchases
        conversionRate
        revenueGenerated
      }
    }
    monthlyTrends {
      dataPoints {
        month
        consumersSampled
        purchases
        conversionRate
        revenue
        recapsCount
      }
    }
    performanceInsights {
      newCustomersSampled
      newCustomersPercentage
      brandAwareness
      brandAwarenessPercentage
      willingToPurchase
      willingToPurchasePercentage
      bestMonth {
        month
        recapsCount
        consumersCount
      }
      growthRate
    }
    marketAnalysis {
      dataPoints {
        marketId
        marketName
        consumers
        purchases
        conversion
        demos
        efficiency
      }
    }
    rmmPerformance {
      dataPoints {
        rmmId
        rmmName
        consumersSampled
        demos
        conversionRate
      }
    }
  }
}
```

**Parameters:**
- `filters` (RecapDashboardFiltersInput, optional): Filter options (see [Filtering](#filtering))

**Response Fields:**

#### Metrics (RecapDashboardMetrics)
- `totalConsumersSampled` (int): Total consumers sampled (sum of `total_consumer` from ConsumerEngagements)
- `totalPurchases` (int): Total purchases (sum of `products_sold` from Recap)
- `conversionRate` (float): Conversion rate percentage (0-100) - willing to purchase / total consumers
- `revenueGenerated` (float): Total revenue generated (sum of `total_earnings` from Recap)
- `comparisonPeriod` (str, optional): Comparison period string (e.g., "Q4 2024") - shown when quarter filter is used
- `comparisonValues` (RecapComparisonValues, optional): Previous period comparison values
  - `totalConsumersSampled` (int): Consumers sampled in comparison period
  - `totalPurchases` (int): Purchases in comparison period
  - `conversionRate` (float): Conversion rate in comparison period
  - `revenueGenerated` (float): Revenue in comparison period

#### Monthly Trends (RecapMonthlyTrends)
- `dataPoints` (List[RecapMonthlyDataPoint]): Monthly performance data points
  - `month` (str): Month string (e.g., "2025-01")
  - `consumersSampled` (int): Total consumers sampled in this month
  - `purchases` (int): Total purchases in this month
  - `conversionRate` (float): Conversion rate percentage (0-100)
  - `revenue` (float): Total revenue in this month
  - `recapsCount` (int): Number of recaps in this month

#### Performance Insights (RecapPerformanceInsights)
- `newCustomersSampled` (int): Total first-time consumers sampled
- `newCustomersPercentage` (float): Percentage of first-time consumers (0-100)
- `brandAwareness` (int): Total consumers who knew about the brand
- `brandAwarenessPercentage` (float): Percentage of consumers who knew about the brand (0-100)
- `willingToPurchase` (int): Total consumers willing to purchase
- `willingToPurchasePercentage` (float): Percentage of consumers willing to purchase (0-100)
- `bestMonth` (BestRecapMonth, optional): Best performing month based on consumers sampled
  - `month` (str): Month string (e.g., "2025-06")
  - `recapsCount` (int): Number of recaps in best month
  - `consumersCount` (int): Consumers sampled in best month
- `growthRate` (float): Growth rate percentage - recaps growth vs same quarter last year (can be negative)

#### Market Analysis (MarketPerformanceAnalysis)
- `dataPoints` (List[MarketPerformanceData]): Market performance data points grouped by retailer
  - `marketId` (ID): Retailer ID
  - `marketName` (str): Retailer name
  - `consumers` (int): Total consumers in this market
  - `purchases` (int): Total purchases in this market
  - `conversion` (float): Conversion rate percentage (0-100)
  - `demos` (int): Total demos/engagements in this market
  - `efficiency` (float): Market efficiency percentage (0-100) - purchases / consumers * 100

#### RMM Performance (RMMPerformance)
- `dataPoints` (List[RMMPerformanceData]): RMM performance data points grouped by retailer
  - `rmmId` (ID): Retailer ID
  - `rmmName` (str): Retailer name
  - `consumersSampled` (int): Total consumers sampled for this RMM
  - `demos` (int): Total demos/engagements for this RMM
  - `conversionRate` (float): Conversion rate percentage (0-100)

**Default Behavior:**
- If no filters are provided, defaults to **current quarter** for all tenants
- Shows **all data across all tenants** by default (admin dashboard)
- Only filters by tenant if `tenantId` is explicitly provided in filters

---

## Input Types

### EventDashboardFiltersInput

The Event Dashboard query accepts an optional `filters` parameter of type `EventDashboardFiltersInput`:

```graphql
input EventDashboardFiltersInput {
  # Tenant filter (optional - admin dashboard shows all by default)
  tenantId: ID
  
  # Date range filters
  startDate: String  # ISO date string (YYYY-MM-DD)
  endDate: String    # ISO date string (YYYY-MM-DD)
  
  # Quarter filter (takes precedence over startDate/endDate if provided)
  quarter: String    # Quarter string like "Q1 2025"
  
  # RMM filter (RMM = Retailer)
  rmmId: ID          # Retailer ID
  
  # Distributor filter
  distributorId: ID
}
```

**Filter Priority:**
1. If `quarter` is provided, it takes precedence over `startDate`/`endDate`
2. If only `startDate`/`endDate` are provided, they are used
3. If neither is provided, defaults to **current quarter**

**All filters are optional**. When not provided, queries will return data for all tenants within the current quarter.

### RecapDashboardFiltersInput

The Recap Dashboard query accepts an optional `filters` parameter of type `RecapDashboardFiltersInput`:

```graphql
input RecapDashboardFiltersInput {
  # Tenant filter (optional - admin dashboard shows all by default)
  tenantId: ID
  
  # Date range filters
  startDate: String  # ISO date string (YYYY-MM-DD)
  endDate: String    # ISO date string (YYYY-MM-DD)
  
  # Quarter filter (takes precedence over startDate/endDate if provided)
  quarter: String    # Quarter string like "Q1 2025"
  
  # RMM filter (RMM = Retailer)
  rmmId: ID          # Retailer ID
  
  # Distributor filter
  distributorId: ID
}
```

**Filter Priority:**
1. If `quarter` is provided, it takes precedence over `startDate`/`endDate`
2. If only `startDate`/`endDate` are provided, they are used
3. If neither is provided, defaults to **current quarter**

**All filters are optional**. When not provided, queries will return data for all tenants within the current quarter.

---

## Response Types

### EventDashboardFilterOptions

```graphql
type EventDashboardFilterOptions {
  distributors: [DistributorOption!]
  rmms: [RetailerOption!]  # RMM = Retailer
  quarters: [QuarterOption!]
  tenants: [TenantOption!]
}

type DistributorOption {
  id: ID!
  name: String!
}

type RetailerOption {
  id: ID!
  name: String!
  address: String!
}

type QuarterOption {
  value: String!  # e.g., "Q1 2025"
  label: String! # e.g., "Q1 2025"
}

type TenantOption {
  id: ID!
  name: String!
}
```

### EventDashboard

```graphql
type EventDashboard {
  metrics: EventDashboardMetrics!
  monthlyTrends: MonthlyPerformanceTrend!
  performanceInsights: PerformanceInsights!
  recentEvents: [RecentEvent!]
}

type EventDashboardMetrics {
  totalEvents: Int!
  consumersSampled: Int!
  brandAwareness: Float!  # Percentage (0-100)
  purchaseIntent: Float!  # Percentage (0-100)
  comparisonPeriod: String  # e.g., "Q4 2024"
  comparisonValues: ComparisonValues
}

type ComparisonValues {
  totalEvents: Int!
  consumersSampled: Int!
  brandAwareness: Float!
  purchaseIntent: Float!
}

type MonthlyPerformanceTrend {
  dataPoints: [MonthlyDataPoint!]!
}

type MonthlyDataPoint {
  month: String!  # e.g., "2025-01"
  consumersSampled: Int!
  willingToPurchase: Int!
  conversionRate: Float!  # Percentage (0-100)
  eventsCount: Int!
}

type PerformanceInsights {
  knewAboutBrand: Int!
  knewAboutBrandPercentage: Float!  # Percentage (0-100)
  willingToPurchase: Int!
  willingToPurchasePercentage: Float!  # Percentage (0-100)
  bestMonth: BestMonth
  growthRate: Float!  # Percentage (can be negative)
}

type BestMonth {
  month: String!  # e.g., "2025-06"
  eventsCount: Int!
  consumersCount: Int!
}

type RecentEvent {
  id: ID!
  name: String!
  date: String!  # ISO date string
  location: String!  # RMM/Retailer name
  consumers: Int!
  intentRate: Float!  # Percentage (0-100)
  status: String!  # "Upcoming", "Completed", etc.
}
```

### RecapDashboardFilterOptions

```graphql
type RecapDashboardFilterOptions {
  distributors: [DistributorOption!]
  rmms: [RetailerOption!]  # RMM = Retailer
  quarters: [QuarterOption!]
  tenants: [TenantOption!]
}
```

(Reuses the same filter option types as Event Dashboard: `DistributorOption`, `RetailerOption`, `QuarterOption`, `TenantOption`)

### RecapDashboard

```graphql
type RecapDashboard {
  metrics: RecapDashboardMetrics!
  monthlyTrends: RecapMonthlyTrends!
  performanceInsights: RecapPerformanceInsights!
  marketAnalysis: MarketPerformanceAnalysis!
  rmmPerformance: RMMPerformance!
}

type RecapDashboardMetrics {
  totalConsumersSampled: Int!
  totalPurchases: Int!
  conversionRate: Float!  # Percentage (0-100)
  revenueGenerated: Float!
  comparisonPeriod: String  # e.g., "Q4 2024"
  comparisonValues: RecapComparisonValues
}

type RecapComparisonValues {
  totalConsumersSampled: Int!
  totalPurchases: Int!
  conversionRate: Float!
  revenueGenerated: Float!
}

type RecapMonthlyTrends {
  dataPoints: [RecapMonthlyDataPoint!]!
}

type RecapMonthlyDataPoint {
  month: String!  # e.g., "2025-01"
  consumersSampled: Int!
  purchases: Int!
  conversionRate: Float!  # Percentage (0-100)
  revenue: Float!
  recapsCount: Int!
}

type RecapPerformanceInsights {
  newCustomersSampled: Int!
  newCustomersPercentage: Float!  # Percentage (0-100)
  brandAwareness: Int!
  brandAwarenessPercentage: Float!  # Percentage (0-100)
  willingToPurchase: Int!
  willingToPurchasePercentage: Float!  # Percentage (0-100)
  bestMonth: BestRecapMonth
  growthRate: Float!  # Percentage (can be negative)
}

type BestRecapMonth {
  month: String!  # e.g., "2025-06"
  recapsCount: Int!
  consumersCount: Int!
}

type MarketPerformanceAnalysis {
  dataPoints: [MarketPerformanceData!]!
}

type MarketPerformanceData {
  marketId: ID!  # Retailer ID
  marketName: String!
  consumers: Int!
  purchases: Int!
  conversion: Float!  # Percentage (0-100)
  demos: Int!
  efficiency: Float!  # Percentage (0-100)
}

type RMMPerformance {
  dataPoints: [RMMPerformanceData!]!
}

type RMMPerformanceData {
  rmmId: ID!  # Retailer ID
  rmmName: String!
  consumersSampled: Int!
  demos: Int!
  conversionRate: Float!  # Percentage (0-100)
}
```

---

## Filtering

Both Event Dashboard and Recap Dashboard support comprehensive filtering through their respective filter input types (`EventDashboardFiltersInput` and `RecapDashboardFiltersInput`). Filters are applied at the database level for optimal performance.

### Tenant Filtering

**Important**: The Event Dashboard is an **admin dashboard** that shows all data across all tenants by default. Only filter by tenant when you need tenant-specific data:

```graphql
filters: {
  tenantId: "123"  # Filter to specific tenant
}
```

**Default behavior**: If `tenantId` is not provided, shows data for **all tenants**.

### Quarter Filtering

Filter by quarter (takes precedence over date range):

```graphql
filters: {
  quarter: "Q1 2025"  # Quarter format: "Q{1-4} {YYYY}"
}
```

**Available quarters**: Last 2 years + current year (e.g., Q1 2023 through Q4 2025)

### Date Range Filtering

Use `startDate` and `endDate` to filter by event date (only if quarter is not provided):

```graphql
filters: {
  startDate: "2024-01-01"  # ISO date format (YYYY-MM-DD)
  endDate: "2024-12-31"
}
```

**Default behavior**: If neither quarter nor date range is provided, defaults to **current quarter**.

### Distributor Filtering

Filter by distributor:

```graphql
filters: {
  distributorId: "456"
}
```

### RMM (Retailer) Filtering

Filter by retailer/RMM:

```graphql
filters: {
  rmmId: "789"  # Retailer ID
}
```

**Note**: RMM stands for Retailer in this context.

### Combining Filters

All filters can be combined:

```graphql
filters: {
  tenantId: "123"
  quarter: "Q1 2025"
  distributorId: "456"
  rmmId: "789"
}
```

---

## Performance Considerations

Both Dashboard APIs are optimized for performance using several techniques:

### Database-Level Aggregations

All statistics are calculated at the database level using Django ORM aggregations (`Count`, `Sum`, `Q`, etc.), not in Python. This ensures efficient query execution.

### Efficient Joins

Queries use `select_related()` for ForeignKey relationships and `prefetch_related()` for ManyToMany and reverse ForeignKey relationships to minimize database queries.

### Caching

- **Filter Options**: Cached for 1 hour (rarely changes)
- **Event Dashboard**: Cached for 10 minutes (frequently accessed, needs freshness)
- **Recap Dashboard**: Cached for 10 minutes (frequently accessed, needs freshness)
- Cache keys include all filter parameters to ensure correct cache hits/misses

### Filtering

Filters are applied early in the query chain to reduce the dataset size before aggregations.

### Recommended Practices

1. **Use quarter filters**: Prefer quarter filters over date ranges for better performance and consistency
2. **Combine filters**: Use multiple filters to narrow down results and improve performance
3. **Cache filter options**: Cache the filter options query result on the client side (changes rarely)
4. **Limit date ranges**: If using date ranges instead of quarters, limit to reasonable periods (e.g., max 1 year)

---

## Examples

### Example 1: Get Filter Options

Get all available filter options for the Event Dashboard:

```graphql
query GetFilterOptions {
  eventDashboardFilterOptions {
    distributors {
      id
      name
    }
    rmms {
      id
      name
      address
    }
    quarters {
      value
      label
    }
    tenants {
      id
      name
    }
  }
}
```

### Example 2: Current Quarter Dashboard (All Tenants)

Get Event Dashboard data for the current quarter across all tenants:

```graphql
query CurrentQuarterDashboard {
  eventDashboard {
    metrics {
      totalEvents
      consumersSampled
      brandAwareness
      purchaseIntent
    }
    monthlyTrends {
      dataPoints {
        month
        consumersSampled
        conversionRate
        eventsCount
      }
    }
    performanceInsights {
      knewAboutBrand
      knewAboutBrandPercentage
      willingToPurchase
      willingToPurchasePercentage
      bestMonth {
        month
        eventsCount
      }
      growthRate
    }
    recentEvents {
      id
      name
      date
      location
      consumers
      intentRate
    }
  }
}
```

### Example 3: Specific Quarter with Comparison

Get Event Dashboard data for Q1 2025 with period-over-period comparison:

```graphql
query Q1Dashboard {
  eventDashboard(
    filters: {
      quarter: "Q1 2025"
    }
  ) {
    metrics {
      totalEvents
      consumersSampled
      brandAwareness
      purchaseIntent
      comparisonPeriod
      comparisonValues {
        totalEvents
        consumersSampled
        brandAwareness
        purchaseIntent
      }
    }
    monthlyTrends {
      dataPoints {
        month
        consumersSampled
        willingToPurchase
        conversionRate
      }
    }
    performanceInsights {
      growthRate
      bestMonth {
        month
        eventsCount
        consumersCount
      }
    }
  }
}
```

### Example 4: Tenant-Specific Dashboard

Get Event Dashboard data for a specific tenant:

```graphql
query TenantDashboard {
  eventDashboard(
    filters: {
      tenantId: "123"
      quarter: "Q1 2025"
    }
  ) {
    metrics {
      totalEvents
      consumersSampled
      brandAwareness
      purchaseIntent
    }
    monthlyTrends {
      dataPoints {
        month
        consumersSampled
        conversionRate
      }
    }
    performanceInsights {
      knewAboutBrandPercentage
      willingToPurchasePercentage
      growthRate
    }
  }
}
```

### Example 5: Distributor and RMM Filtered Dashboard

Get Event Dashboard data filtered by distributor and retailer:

```graphql
query FilteredDashboard {
  eventDashboard(
    filters: {
      quarter: "Q1 2025"
      distributorId: "456"
      rmmId: "789"
    }
  ) {
    metrics {
      totalEvents
      consumersSampled
      brandAwareness
      purchaseIntent
    }
    monthlyTrends {
      dataPoints {
        month
        consumersSampled
        conversionRate
        eventsCount
      }
    }
    performanceInsights {
      bestMonth {
        month
        eventsCount
        consumersCount
      }
    }
    recentEvents {
      id
      name
      date
      location
      consumers
      intentRate
    }
  }
}
```

### Example 6: Date Range Dashboard

Get Event Dashboard data for a custom date range:

```graphql
query DateRangeDashboard {
  eventDashboard(
    filters: {
      startDate: "2024-01-01"
      endDate: "2024-03-31"
      tenantId: "123"
    }
  ) {
    metrics {
      totalEvents
      consumersSampled
      brandAwareness
      purchaseIntent
    }
    monthlyTrends {
      dataPoints {
        month
        consumersSampled
        conversionRate
      }
    }
    performanceInsights {
      growthRate
    }
  }
}
```

### Example 7: Recap Dashboard Filter Options

Get all available filter options for the Recap Dashboard:

```graphql
query GetRecapFilterOptions {
  recapDashboardFilterOptions {
    distributors {
      id
      name
    }
    rmms {
      id
      name
      address
    }
    quarters {
      value
      label
    }
    tenants {
      id
      name
    }
  }
}
```

### Example 8: Current Quarter Recap Dashboard

Get Recap Dashboard data for the current quarter across all tenants:

```graphql
query CurrentQuarterRecapDashboard {
  recapDashboard {
    metrics {
      totalConsumersSampled
      totalPurchases
      conversionRate
      revenueGenerated
    }
    monthlyTrends {
      dataPoints {
        month
        consumersSampled
        purchases
        conversionRate
        revenue
        recapsCount
      }
    }
    performanceInsights {
      newCustomersSampled
      newCustomersPercentage
      brandAwareness
      brandAwarenessPercentage
      willingToPurchase
      willingToPurchasePercentage
      growthRate
    }
    marketAnalysis {
      dataPoints {
        marketName
        consumers
        purchases
        conversion
        demos
        efficiency
      }
    }
    rmmPerformance {
      dataPoints {
        rmmName
        consumersSampled
        demos
        conversionRate
      }
    }
  }
}
```

### Example 9: Recap Dashboard with Market Analysis

Get Recap Dashboard data with detailed market analysis:

```graphql
query RecapMarketAnalysis {
  recapDashboard(
    filters: {
      quarter: "Q1 2025"
      distributorId: "456"
    }
  ) {
    metrics {
      totalConsumersSampled
      totalPurchases
      conversionRate
      revenueGenerated
    }
    marketAnalysis {
      dataPoints {
        marketId
        marketName
        consumers
        purchases
        conversion
        demos
        efficiency
      }
    }
    rmmPerformance {
      dataPoints {
        rmmId
        rmmName
        consumersSampled
        demos
        conversionRate
      }
    }
  }
}
```

### Example 10: Recap Dashboard with Comparison

Get Recap Dashboard data for Q1 2025 with period-over-period comparison:

```graphql
query RecapComparison {
  recapDashboard(
    filters: {
      quarter: "Q1 2025"
    }
  ) {
    metrics {
      totalConsumersSampled
      totalPurchases
      conversionRate
      revenueGenerated
      comparisonPeriod
      comparisonValues {
        totalConsumersSampled
        totalPurchases
        conversionRate
        revenueGenerated
      }
    }
    performanceInsights {
      newCustomersSampled
      brandAwareness
      willingToPurchase
      growthRate
      bestMonth {
        month
        recapsCount
        consumersCount
      }
    }
  }
}
```

---

## Error Handling

All queries will return errors in the standard GraphQL error format if:

- Authentication fails (missing or invalid JWT token)
- Invalid filter values are provided (e.g., invalid quarter format)
- Invalid date formats are provided

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

### Admin Dashboard Behavior

- **Default**: Shows all data across all tenants (admin view)
- **Tenant Filtering**: Only filter by tenant when `tenantId` is explicitly provided
- **No Automatic Tenant Filtering**: Unlike other queries, both dashboards do NOT automatically filter by the authenticated user's tenant

### Quarter Format

- Quarters must be in format: `"Q{1-4} {YYYY}"` (e.g., "Q1 2025", "Q4 2024")
- Quarter filter takes precedence over date range filters
- Defaults to current quarter if no date/quarter filter is provided

### Event Dashboard Data Sources

- **Events**: Only events with recaps are included in calculations
- **Consumer Data**: Comes from `ConsumerEngagements` model linked to recaps
- **Brand Awareness**: Calculated from `brand_aware_consumers` / `total_consumer`
- **Purchase Intent**: Calculated from `willing_to_purchase_consumers` / `total_consumer`

### Recap Dashboard Data Sources

- **Recaps**: All recaps are included in calculations
- **Consumer Data**: Comes from `ConsumerEngagements` model linked to recaps
- **Purchases**: Sum of `products_sold` from Recap model
- **Revenue**: Sum of `total_earnings` from Recap model
- **Demos**: Sum of `total_engagements` from Recap model
- **Brand Awareness**: Calculated from `brand_aware_consumers` / `total_consumer`
- **Purchase Intent**: Calculated from `willing_to_purchase_consumers` / `total_consumer`
- **New Customers**: Sum of `first_time_consumers` from ConsumerEngagements
- **Market Analysis**: Grouped by retailer (from `recap.retailer` or `event.request.retailer`)
- **RMM Performance**: Grouped by retailer with consumers, demos, and conversion rates

### Percentages

- All percentages are returned as floats (0-100), not decimals (0-1)
- Brand awareness and purchase intent are calculated as percentages of total consumers sampled
- Conversion rate is calculated as: `willing_to_purchase_consumers / total_consumer * 100`
- Market efficiency is calculated as: `purchases / consumers * 100`

### Recent Events (Event Dashboard Only)

- Shows upcoming events (events with `start_time` or `date` in the future)
- Limited to 10 most recent upcoming events
- Includes consumer engagement data from recaps

### Market Analysis (Recap Dashboard Only)

- Groups performance data by retailer/market
- Retailer can come from `recap.retailer` (preferred) or `event.request.retailer` (fallback)
- Efficiency metric shows purchase efficiency per consumer
- Data points are sorted by consumers (descending)

### RMM Performance (Recap Dashboard Only)

- Groups performance data by retailer/RMM
- Shows consumers sampled, demos (total engagements), and conversion rate
- Data points are sorted by consumers sampled (descending)

### Caching

- Filter options are cached for 1 hour
- Event Dashboard data is cached for 10 minutes
- Recap Dashboard data is cached for 10 minutes
- Cache keys include all filter parameters to ensure correct cache behavior
- Cache is automatically invalidated when data changes

### RMM Terminology

- **RMM** stands for **Retailer** in this context
- RMM filter uses `rmmId` which is actually a Retailer ID
- Retailer information comes from `recap.retailer` (Recap Dashboard) or `request.retailer` (Event Dashboard)
