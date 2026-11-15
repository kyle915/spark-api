"""The web app's own GraphQL documents, verbatim, for the dismissal tests.

Copied from spark-front-client (src/api/queries, src/api/mutations) so the
tests run the exact field set the pages send — a resolver that only works
for a hand-trimmed selection is the failure mode these guard against.
Regenerate when those documents change.
"""

# src/api/queries/MissingRecapEventsQuery.ts
MISSING_Q = """
  query MissingRecapEventsQuery($tenantId: ID, $lookbackDays: Int!) {
    missingRecapEvents(tenantId: $tenantId, lookbackDays: $lookbackDays) {
      eventUuid
      eventName
      venue
      address
      stateCode
      date
      startTime
      endTime
      hoursOverdue
      requestUuid
      assignedAmbassadors {
        ambassadorEventUuid
        ambassadorUuid
        name
        email
        isApproved
      }
    }
  }
"""

# src/api/queries/SidebarCountsQuery.ts
SIDEBAR_Q = """
  query SidebarCountsQuery($tenantId: ID) {
    sidebarRequestCounts(tenantId: $tenantId) {
      tracker
      approvals
      approvalsSlaBreach
      upcoming
      done30d
      recapsDue
    }
    sidebarAlertCandidates(tenantId: $tenantId) {
      id
      createdAt
      updatedAt
      statusSlug
    }
    pendingWalkupCount(tenantId: $tenantId)
    recapsNeedsReview: recapTypeCounts(
      filters: { tenantId: $tenantId, approved: false, archived: false }
    ) {
      total
    }
  }
"""

# src/api/queries/DismissedMissingRecapsQuery.ts
DISMISSED_Q = """
  query DismissedMissingRecapsQuery($tenantId: ID, $lookbackDays: Int!) {
    dismissedMissingRecaps(tenantId: $tenantId, lookbackDays: $lookbackDays) {
      dismissalUuid
      eventUuid
      eventName
      address
      stateCode
      date
      endTime
      ambassadorName
      ambassadorEmail
      reason
      reasonLabel
      note
      dismissedByName
      dismissedAt
      recapFiled
    }
  }
"""

# src/api/mutations/DismissMissingRecapsMutation.ts
DISMISS_M = """
  mutation DismissMissingRecapsMutation($input: DismissMissingRecapsInput!) {
    dismissMissingRecaps(input: $input) {
      success
      message
      dismissedCount
      dismissalUuids
    }
  }
"""

# src/api/mutations/RestoreMissingRecapsMutation.ts
RESTORE_M = """
  mutation RestoreMissingRecapsMutation($input: RestoreMissingRecapsInput!) {
    restoreMissingRecaps(input: $input) {
      success
      message
      restoredCount
    }
  }
"""

# src/api/mutations/NudgeAmbassadorForRecapMutation.ts
NUDGE_M = """
  mutation NudgeAmbassadorForRecapMutation(
    $input: NudgeAmbassadorForRecapInput!
  ) {
    nudgeAmbassadorForRecap(input: $input) {
      success
      message
      devicesNotified
    }
  }
"""

# src/api/queries/TrackerListQuery.ts
TRACKER_Q = """
  query TrackerListQuery(
    $first: Int
    $after: String
    $q: String
    $filters: RequestFiltersInput
  ) {
    requests(first: $first, after: $after, q: $q, filters: $filters) {
      pageInfo {
        hasNextPage
        endCursor
      }
      edges {
        cursor
        node {
          id
          uuid
          name
          date
          startTime
          endTime
          address
          market
          schedulingStatus
          isNonActiveProductRequired
          activationPlan {
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
          productNames
          openShiftsOpen
          openShiftsTotal
          openShiftsTooltip
          status {
            id
            name
            slug
          }
          # RMM owner for Master Tracker "By RMM" group headers.
          rmmAsigned {
            id
            firstName
            lastName
          }
          event {
            id
            uuid
            date
            startTime
            assignedAmbassadorsCount
            confirmedAmbassadorsCount
          }
          recapsFiledCount
          recapEventUuid
          eventsCount
          # True when staff dismissed every event on the request as owing
          # no recap, so the row does not show "Recap due".
          recapDismissed
        }
      }
    }
  }
"""

# src/api/queries/TenantProgramHealthQuery.ts
PROGRAM_HEALTH_Q = """
  query TenantProgramHealthQuery(
    $tenantId: ID!
    $startDate: String
    $endDate: String
  ) {
    tenantProgramHealth(
      tenantId: $tenantId
      startDate: $startDate
      endDate: $endDate
    ) {
      scheduled
      filed
      approved
      missing
      startDate
      endDate
    }
  }
"""
