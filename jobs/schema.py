import strawberry

from jobs import mutations, queries


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
    queries.CompanyToAmbassadorReviewQueries,
    queries.AmbassadorToAmbassadorReviewQueries,
    queries.QuestionTypeQueries,
    queries.JobRequirementQuestionQueries,
    queries.QuestionOptionQueries,
    queries.JobRequirementAnswerQueries,
):
    pass


@strawberry.type
class SparkJobQueries(
    ClientJobQueries
):
    pass


@strawberry.type
class ClientJobMutations(
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
    mutations.AmbassadorJobMutations,
    mutations.CompanyToAmbassadorReviewMutations,
    mutations.AmbassadorToAmbassadorReviewMutations,
    mutations.QuestionTypeMutations,
    mutations.JobRequirementQuestionMutations,
    mutations.QuestionOptionMutations,
    mutations.JobRequirementAnswerMutations,
):
    pass


@strawberry.type
class SparkJobMutations(
    ClientJobMutations
):
    pass
