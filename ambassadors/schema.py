import strawberry

from ambassadors import queries, mutations


@strawberry.type
class AmbassadorQuerySpark(
    queries.FileTypeQueries,
    queries.AmbassadorEventQueries,
    queries.AmbassadorManagementQueries,
    queries.AmbassadorProfileQueries,
    queries.AmbassadorReviewQueries,
    queries.AmbassadorNoteQueries,
    queries.SkillQueries,
    queries.AmbassadorSkillQueries,
    queries.AttendanceQueries,
):
    pass


@strawberry.type
class AmbassadorQueryClient(
    queries.FileTypeQueries,
    queries.AmbassadorEventQueries,
    queries.AmbassadorManagementQueries,
    queries.AmbassadorProfileQueries,
    queries.AmbassadorReviewQueries,
    queries.AmbassadorNoteQueries,
    queries.SkillQueries,
    queries.AmbassadorSkillQueries,
    queries.AttendanceQueries,
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
    mutations.AttendanceMutations,
):
    pass
