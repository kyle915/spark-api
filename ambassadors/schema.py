import strawberry

from ambassadors import queries, mutations


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
):
    pass
