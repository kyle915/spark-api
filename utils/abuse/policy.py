"""Which GraphQL root fields fall into which guardrail bucket, and the limits.

Every number is a Django setting (env-overridable in config/settings.py);
``limits()`` reads them at call time so tests can override with
``settings.ABUSE_...``.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

# --- field groups (camelCase root field names on the clients schema) -------

# Ambassador list / search / detail reads. Brand users get a tighter bucket
# here (plus a distinct-BAs-per-day ceiling) so they can click around but
# can't walk the whole BA pool.
AMBASSADOR_LIST_FIELDS = frozenset({
    "activeAmbassadors", "pendingAmbassadors", "ambassadors", "availableAmbassadors",
    "ambassador", "ambassadorProfile", "ambassadorProfileDetail", "ambassadorGigHistory",
    "ambassadorReviews", "ambassadorReview", "ambassadorRatings",
    "ambassadorNotes", "ambassadorNote", "ambassadorSkills", "ambassadorSkill",
    "ambassadorGroups", "ambassadorGroup", "invitedGroupsByJob",
    "ambassadorsBookedOnDate", "staffingSearch", "staffingSuggestions", "eventStaffing",
    "favoriteAmbassadors", "jobApplications", "jobApplicants", "chatRecipientAmbassadors",
    "companyToAmbassadorReviews", "ambassadorToAmbassadorReviews", "attendances",
    "eventLocationTrail", "sentInvitations", "talentSearch",
})

# Fields whose results carry BA identities but are dashboards/rosters that
# the FE polls or loads per page view: NOT bucketed (so normal use can never
# trip a limit) but their BA reads still feed the distinct-ambassador log.
AMBASSADOR_LOGGED_FIELDS = AMBASSADOR_LIST_FIELDS | frozenset({
    "ambassadorEvents", "staffingBoard", "liveShiftBoard", "walkupShifts",
    "eventAttendance", "recentLocationPings", "tenantBaLeaderboard",
})

# GraphQL object types that ARE a BA record (their uuid/id is the BA key).
AMBASSADOR_TYPES = frozenset({"Ambassador", "AmbassadorProfile", "AmbassadorProfileDetail"})
# Scalar fields anywhere in a logged result that name a BA.
AMBASSADOR_KEY_FIELDS = frozenset({"ambassadorId", "ambassadorUuid"})

EXPORT_FIELDS = frozenset({
    "exportRecapsXlsx", "exportRecapXlsx", "exportCustomRecapsXlsx", "exportCustomRecapXlsx",
    "exportExpenseReceipts", "generateCampaignReportPdf", "generateRecapPdf",
    "generateCustomRecapPdf", "clientRecapDraftPdf", "shareRecapsByEmail", "shareRecapByEmail",
})

BULK_FIELDS = frozenset({
    "bulkApproveRecaps", "markRecapsShared", "importRequestsBatch", "bulkCloneRequest",
    "bulkAssignAmbassadorToEvents", "bulkInviteAmbassadorsToShift",
    "bulkConfirmWalkupShifts", "bulkRejectWalkupShifts", "inviteAmbassadorsToJob",
    "addAmbassadorsToGroup", "removeAmbassadorsFromGroup", "inviteClientUsers",
    "regenerateAmbassadorPasswords", "shareRecapsByEmail",
})

DOWNLOAD_FIELDS = frozenset({
    "recapFileDownloadUrl", "getDownloadUrl", "requestBatchTemplateDownloadUrl",
    "tenantPhotoGallery",
})

LOGIN_FIELDS = frozenset({
    "tokenAuth", "requestMagicLink", "loginWithMagicToken", "requestPasswordReset",
    "confirmPasswordReset", "forgotPassword", "resetPasswordWithCode",
})

PAGE_ARG_NAMES = frozenset({"first", "last", "limit", "pageSize"})

THROTTLE_MESSAGES = {
    "RATE_LIMITED": "Too many requests — slow down and try again in a minute.",
    "EXPORT_LIMIT": "Export limit reached. Try again later.",
    "BULK_LIMIT": "Too many bulk actions in a short time. Try again later.",
    "LOGIN_THROTTLED": "Too many sign-in attempts. Please wait a few minutes and try again.",
}


def _s(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


@dataclass(frozen=True)
class Limits:
    user_per_minute: int
    user_per_hour: int
    admin_per_minute: int
    admin_per_hour: int
    anon_ip_per_minute: int
    anon_ip_per_hour: int
    ambassador_per_minute: int
    ambassador_per_hour: int
    ambassador_per_day: int
    ambassador_distinct_per_day: int
    exports_per_hour: int
    exports_per_day: int
    tenant_exports_per_day: int
    admin_exports_per_hour: int
    bulk_per_hour: int
    admin_bulk_per_hour: int
    bulk_max_items: int
    admin_bulk_max_items: int
    downloads_per_hour: int
    login_ip_per_15m: int
    login_identifier_per_15m: int
    client_page_cap: int
    max_depth: int
    max_aliases: int
    max_tokens: int


def limits() -> Limits:
    return Limits(
        user_per_minute=_s("ABUSE_GQL_USER_PER_MINUTE", 120),
        user_per_hour=_s("ABUSE_GQL_USER_PER_HOUR", 3000),
        admin_per_minute=_s("ABUSE_GQL_ADMIN_PER_MINUTE", 600),
        admin_per_hour=_s("ABUSE_GQL_ADMIN_PER_HOUR", 15000),
        anon_ip_per_minute=_s("ABUSE_GQL_ANON_IP_PER_MINUTE", 120),
        anon_ip_per_hour=_s("ABUSE_GQL_ANON_IP_PER_HOUR", 2000),
        ambassador_per_minute=_s("ABUSE_AMBASSADOR_PER_MINUTE", 30),
        ambassador_per_hour=_s("ABUSE_AMBASSADOR_PER_HOUR", 300),
        ambassador_per_day=_s("ABUSE_AMBASSADOR_PER_DAY", 1000),
        ambassador_distinct_per_day=_s("ABUSE_AMBASSADOR_DISTINCT_PER_DAY", 500),
        exports_per_hour=_s("ABUSE_EXPORTS_PER_HOUR", 20),
        exports_per_day=_s("ABUSE_EXPORTS_PER_DAY", 100),
        tenant_exports_per_day=_s("ABUSE_TENANT_EXPORTS_PER_DAY", 300),
        admin_exports_per_hour=_s("ABUSE_ADMIN_EXPORTS_PER_HOUR", 200),
        bulk_per_hour=_s("ABUSE_BULK_PER_HOUR", 60),
        admin_bulk_per_hour=_s("ABUSE_ADMIN_BULK_PER_HOUR", 600),
        bulk_max_items=_s("ABUSE_BULK_MAX_ITEMS", 200),
        admin_bulk_max_items=_s("ABUSE_ADMIN_BULK_MAX_ITEMS", 2000),
        downloads_per_hour=_s("ABUSE_DOWNLOADS_PER_HOUR", 600),
        login_ip_per_15m=_s("ABUSE_LOGIN_IP_PER_15M", 50),
        login_identifier_per_15m=_s("ABUSE_LOGIN_IDENTIFIER_PER_15M", 10),
        client_page_cap=_s("ABUSE_CLIENT_PAGE_CAP", 200),
        max_depth=_s("ABUSE_GQL_MAX_DEPTH", 12),
        max_aliases=_s("ABUSE_GQL_MAX_ALIASES", 20),
        max_tokens=_s("ABUSE_GQL_MAX_TOKENS", 4000),
    )
