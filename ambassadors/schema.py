import strawberry

from ambassadors import queries, mutations
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
):
    pass


@strawberry.type
class AmbassadorMutationsClient(
    AmbassadorMutations,
    mutations.GroupTypeMutations,
    mutations.AmbassadorGroupMutations,
):
    pass


@strawberry.type
class AmbassadorMutationsSpark(
    AmbassadorMutationsClient,
    # Admin notification center (web): mark notifications read + resolve
    # (approve/decline) a BA's shift-extension request.
    mutations.NotificationMutations,
    mutations.ShiftExtensionAdminMutations,
):
    pass
