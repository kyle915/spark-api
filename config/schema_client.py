from strawberry.tools import merge_types
from strawberry_django.optimizer import DjangoOptimizerExtension
# Monitored subclass: unexpected resolver crashes feed the backend
# error monitor (utils.error_monitor) instead of vanishing into a
# masked GraphQL 200.
from utils.graphql.monitored_schema import MonitoredJwtSchema as JwtSchema
from events.schema import EventQueryClient, EventMutationsClient
from recaps.schema import RecapQueryClient, RecapMutationsClient
from recaps.recap_ai import RecapAiMutations, RecapAiQueries
from recaps.feedback_themes import FeedbackThemeMutations, FeedbackThemeQueries
from recaps.comment_insights import CommentInsightQueries
from consumer_mentions.trends import MentionTrendQueries
from photo_library.schema import PhotoLibraryQueries
from recaps.weekly_summary import WeeklySummaryMutations, WeeklySummaryQueries
from brand_insights.schema import BrandInsightMutations, BrandInsightQueries
from brand_chat.schema import BrandChatMutations, BrandChatQueries
from recaps.flag_explanation import RecapFlagExplanationQueries
from recaps.receipt_audit_schema import ReceiptAuditMutations, ReceiptAuditQueries
from recaps.metric_mapping_schema import (
    RecapMetricMappingMutations,
    RecapMetricMappingQueries,
)
from recaps.report_types import (
    CampaignReportQueries,
    ReportSubscriptionQueries,
    FieldSamplingReportMutations,
    ScheduledReportMutations,
    TenantGoalsMutations,
)
from recaps.explain_schema import DashboardExplainQueries
from recaps.list_search import ListSearchQueries
from recaps.month_highlight import MonthHighlightQueries
from recaps.store_profile import StoreProfileQueries
from receipts.schema import ReceiptQueryClient, ReceiptMutationsClient
from billing.schema import BillingQueryClient, BillingMutationsClient
from ambassadors.schema import AmbassadorQueryClient, AmbassadorMutationsClient
from tenants.schema import QueryClients, MutationClients
from tenants.brand_groups import BrandGroupMutations, BrandGroupQueries
from tenants.dashboard.schema import DashboardQueries
from tenants.dashboard.mutations import DashboardMutations
from jobs.schema import ClientJobMutations, ClientJobQueries
from academy.schema import AcademyQueryClient, AcademyMutationsClient
from wingspan.schema import WingspanQueryClient
from chats.schema import ChatQueryClient, ChatMutationsClient
from announcements.schema import AnnouncementQueryClient, AnnouncementMutationsClient
from jobboard.schema import JobBoardMutations, JobBoardQueries
from timeclock.schema import ClockLinkMutations, ClockLinkQueries
from recaps.checkin_issue_schema import CheckinIssueQueries
from ambassadors.product_qa_schema import BaProductQaMutations, BaProductQaQueries
from recaps.shelf_schema import ShelfCheckMutations, ShelfCheckQueries
from recaps.client_deck_schema import (
    BestPhotoMutations,
    BestPhotoQueries,
    ClientRecapDraftMutations,
    ClientRecapDraftQueries,
)
from recap_decks.schema import ClientRecapDeckMutations, ClientRecapDeckQueries
from tenants.notification_pref_schema import (
    NotificationPreferenceMutations,
    NotificationPreferenceQueries,
)
from spark_mcp.schema import AiConnectionMutations, AiConnectionQueries
from sales_assist.schema import SalesAssistMutations, SalesAssistQueries
from utils.utils import BlockIntrospectionForAnonymous
from legal.graphql_extension import LegalAcceptanceGate
from legal.schema import LegalMutations, LegalQueries
from utils.graphql.gcs_schema import GCSQuery
from utils.abuse.graphql_extension import guardrail_extensions

# Clients Schemas
QueryClients = merge_types(
    "Query",
    (
        EventQueryClient,
        RecapQueryClient,
        CampaignReportQueries,
        ReportSubscriptionQueries,
        CheckinIssueQueries,
        BaProductQaQueries,
        ShelfCheckQueries,
        DashboardExplainQueries,
        BestPhotoQueries,
        ClientRecapDraftQueries,
        ClientRecapDeckQueries,
        ListSearchQueries,
        StoreProfileQueries,
        MonthHighlightQueries,
        NotificationPreferenceQueries,
        RecapAiQueries,
        FeedbackThemeQueries,
        CommentInsightQueries,
        MentionTrendQueries,
        PhotoLibraryQueries,
        WeeklySummaryQueries,
        BrandInsightQueries,
        BrandChatQueries,
        RecapFlagExplanationQueries,
        ReceiptAuditQueries,
        RecapMetricMappingQueries,
        AmbassadorQueryClient,
        QueryClients,
        ClientJobQueries,
        DashboardQueries,
        BrandGroupQueries,
        GCSQuery,
        AcademyQueryClient,
        WingspanQueryClient,
        ChatQueryClient,
        AnnouncementQueryClient,
        ReceiptQueryClient,
        BillingQueryClient,
        JobBoardQueries,
        ClockLinkQueries,
        AiConnectionQueries,
        SalesAssistQueries,
        LegalQueries,
    ),
)
MutationClients = merge_types(
    "Mutation",
    (
        EventMutationsClient,
        RecapMutationsClient,
        RecapAiMutations,
        FeedbackThemeMutations,
        ShelfCheckMutations,
        WeeklySummaryMutations,
        BrandInsightMutations,
        BrandChatMutations,
        RecapMetricMappingMutations,
        ReceiptAuditMutations,
        TenantGoalsMutations,
        ScheduledReportMutations,
        NotificationPreferenceMutations,
        FieldSamplingReportMutations,
        BestPhotoMutations,
        ClientRecapDraftMutations,
        ClientRecapDeckMutations,
        MutationClients,
        ClientJobMutations,
        AmbassadorMutationsClient,
        DashboardMutations,
        BrandGroupMutations,
        AcademyMutationsClient,
        ChatMutationsClient,
        AnnouncementMutationsClient,
        ReceiptMutationsClient,
        BillingMutationsClient,
        JobBoardMutations,
        ClockLinkMutations,
        BaProductQaMutations,
        AiConnectionMutations,
        SalesAssistMutations,
        LegalMutations,
    ),
)

schema_clients = JwtSchema(
    query=QueryClients,
    mutation=MutationClients,
    extensions=[
        DjangoOptimizerExtension,
        BlockIntrospectionForAnonymous,
        LegalAcceptanceGate,
        # Anti-scraping guardrails: token/depth/alias limits, per-user rate
        # limits, brand page-size cap, export/bulk caps + audit, usage log.
        *guardrail_extensions(),
    ],
)
