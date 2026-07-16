import strawberry

from ambassadors import queries, mutations, walkup
from ambassadors.staffing import StaffingQueries, StaffingSuggestionQueries


@strawberry.type
class AmbassadorQuerySpark(
    queries.FileTypeQueries,
    queries.AmbassadorEventQueries,
    queries.AmbassadorManagementQueries,
    queries.AmbassadorProfileQueries,
    queries.AmbassadorGigHistoryQueries,
    queries.TalentProfileDetailQueries,
    queries.AmbassadorReviewQueries,
    queries.AmbassadorNoteQueries,
    queries.SkillQueries,
    queries.AmbassadorSkillQueries,
    queries.AttendanceQueries,
    queries.GroupTypeQueries,
    queries.AmbassadorGroupQueries,
    # Walk-up self-serve clock-ins queue (admin review).
    walkup.WalkupAdminQueries,
    # Admin notification center (web): the per-user push log + pending
    # shift-extension requests with inline approve/decline.
    queries.NotificationQueries,
    queries.ShiftExtensionAdminQueries,
):
    pass


@strawberry.type
class AmbassadorQueryClient(
    queries.FileTypeQueries,
    queries.AmbassadorEventQueries,
    queries.AmbassadorManagementQueries,
    queries.AmbassadorProfileQueries,
    queries.AmbassadorGigHistoryQueries,
    queries.TalentProfileDetailQueries,
    queries.AmbassadorReviewQueries,
    queries.AmbassadorNoteQueries,
    queries.SkillQueries,
    queries.AmbassadorSkillQueries,
    queries.AttendanceQueries,
    queries.GroupTypeQueries,
    queries.AmbassadorGroupQueries,
    StaffingQueries,
    StaffingSuggestionQueries,
    # Walk-up self-serve clock-ins queue (admin review) — the web app uses
    # the CLIENTS schema.
    walkup.WalkupAdminQueries,
    # Admin notification center (the web app uses the CLIENTS schema): the
    # per-user push log + pending shift-extension requests.
    queries.NotificationQueries,
    queries.ShiftExtensionAdminQueries,
):
    pass


@strawberry.type
class AmbassadorQueryMobile(
    queries.FileTypeQueries,
    queries.AmbassadorEventQueries,
    queries.AmbassadorProfileQueries,
    queries.AmbassadorReviewQueries,
    queries.AmbassadorNoteQueries,
    queries.SkillQueries,
    queries.AmbassadorSkillQueries,
    queries.AttendanceMobileQueries,
    queries.GroupTypeQueries,
    queries.NotificationQueries,
    queries.ReferralQueries,
    # Walk-up self-serve clock-in: resolve an event code to its event+brand.
    walkup.WalkupMobileQueries,
):
    pass


@strawberry.type
class AmbassadorMutations(
    mutations.AmbassadorMutations,
    mutations.AttendanceMutations,
):
    pass


@strawberry.type
class AmbassadorMutationsMobile(
    mutations.AmbassadorMutations,
    mutations.AmbassadorMobileMutations,
    mutations.AttendanceMutations,
    mutations.ShiftAttendanceMutations,
    mutations.NotificationMutations,
    # Walk-up self-serve clock-in: start a walk-up shift from an event code.
    walkup.WalkupMobileMutations,
):
    pass


@strawberry.type
class AmbassadorMutationsClient(
    AmbassadorMutations,
    mutations.GroupTypeMutations,
    mutations.AmbassadorGroupMutations,
    # Walk-up self-serve clock-ins: generate/revoke event code + confirm/
    # reject a walk-up (the web app uses the CLIENTS schema).
    walkup.WalkupAdminMutations,
    # Admin notification center (the web app uses the CLIENTS schema): mark
    # notifications read + resolve (approve/decline) a shift-extension request.
    mutations.NotificationMutations,
    mutations.ShiftExtensionAdminMutations,
):
    pass


@strawberry.type
class AmbassadorMutationsSpark(
    AmbassadorMutationsClient,
):
    pass
