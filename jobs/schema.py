import strawberry

from jobs import mutations, queries


@strawberry.type
class AmbassadorJobQueries(queries.AmbassadorJobQueries):
    pass


@strawberry.type
class ClientJobQueries(
    queries.StatusQueries,
    queries.CompanyFileQueries,
    queries.CompanyQueries,
    queries.CompanyReviewQueries,
    queries.PayTimingQueries,
    queries.ReviewScoreQueries,
    queries.JobTitleQueries,
    queries.RateTypeQueries,
    queries.RateQueries,
    queries.JobQueries,
    queries.JobFileQueries,
    queries.JobRequirementTypeQueries,
    queries.JobRequirementQueries,
    queries.JobRequirementFileQueries,
    queries.AmbassadorJobQueries,
    queries.ClientSparkAmbassadorJobQueries,
    queries.CompanyToAmbassadorReviewQueries,
    queries.AmbassadorToAmbassadorReviewQueries,
    queries.QuestionTypeQueries,
    queries.JobRequirementQuestionQueries,
    queries.QuestionOptionQueries,
    queries.JobRequirementAnswerQueries,
    queries.BriefingTemplateQueries,
    queries.JobBriefingQueries,
):
    pass


@strawberry.type
class SparkJobQueries(ClientJobQueries):
    pass


@strawberry.type
class AmbassadorJobMutations(
    mutations.AmbassadorJobMutations,
    mutations.AcceptAmbassadorJobInvitationMutations,
    mutations.AmbassadorToAmbassadorReviewMutations,
    mutations.JobApplicationMutations,
):
    pass


@strawberry.type
class ClientJobMutations(
    AmbassadorJobMutations,
    mutations.StatusMutations,
    mutations.CompanyFileMutations,
    mutations.CompanyMutations,
    mutations.CompanyReviewMutations,
    mutations.PayTimingMutations,
    mutations.ReviewScoreMutations,
    mutations.JobTitleMutations,
    mutations.RateTypeMutations,
    mutations.RateMutations,
    mutations.JobMutations,
    mutations.JobFileMutations,
    mutations.JobRequirementTypeMutations,
    mutations.JobRequirementMutations,
    mutations.JobRequirementFileMutations,
    mutations.CompanyToAmbassadorReviewMutations,
    mutations.QuestionTypeMutations,
    mutations.JobRequirementQuestionMutations,
    mutations.QuestionOptionMutations,
    mutations.JobRequirementAnswerMutations,
    mutations.ManageAmbassadorJobMutations,
    mutations.ApproveAmbassadorJobMutations,
    mutations.DeclineAmbassadorJobMutations,
    mutations.JobLifecycleMutations,
    mutations.FavoriteAmbassadorMutations,
    mutations.BriefingTemplateMutations,
    mutations.JobBriefingMutations,
):
    pass


@strawberry.type
class SparkJobMutations(ClientJobMutations):
    pass


@strawberry.type
class MobileJobQueries(ClientJobQueries):
    pass


@strawberry.type
class MobileJobMutations(AmbassadorJobMutations):
    pass
