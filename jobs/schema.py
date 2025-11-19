import strawberry

from jobs import mutations


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
    mutations.StatusMutations,
):
    pass
