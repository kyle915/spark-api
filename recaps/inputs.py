import strawberry
from typing import List

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class RecapFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    event_id: strawberry.ID | None = None
    event_type: strawberry.ID | None = None
    rmm_asigned_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    location_id: strawberry.ID | None = None
    state_id: strawberry.ID | None = None
    event_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    event_address: str | None = None
    edited: bool | None = None


@strawberry.input
class FileRecapCategoryFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None


@strawberry.input
class TypeOfGoodFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None


@strawberry.input
class RecapFileInput(SparkGraphQLInput):
    file: str
    file_type_id: strawberry.ID | None = None
    file_recap_category_id: strawberry.ID | None = None


@strawberry.input
class ConsumerEngagementsInput(SparkGraphQLInput):
    total_consumer: int
    first_time_consumers: int
    brand_aware_consumers: int
    willing_to_purchase_consumers: int
    not_willing_consumers: int


@strawberry.input
class ProductSampleInput(SparkGraphQLInput):
    product_id: strawberry.ID
    quantity: int


@strawberry.input
class SalesPerformanceInput(SparkGraphQLInput):
    product_id: strawberry.ID
    type_of_good_id: strawberry.ID
    price: float


@strawberry.input
class ConsumerFeedbackInput(SparkGraphQLInput):
    demographics: str | None = None
    feedback: str | None = None
    quotes: str | None = None
    positive_stories: str | None = None
    reasons_to_decline: str | None = None


@strawberry.input
class AccountFeedbackInput(SparkGraphQLInput):
    do_differently_feedback: str | None = None
    feedback: str | None = None
    corpo_card: str | None = None
    was_corpo_card_used: bool | None = None


@strawberry.input
class CreateRecapInput(SparkGraphQLInput):
    name: str
    event_id: strawberry.ID
    files: List[RecapFileInput]
    filling_for_ambassador: bool | None = None
    late: bool | None = None
    incomplete: bool | None = None
    
    products_sold: int | None = None
    total_cans_sold: int | None = None
    total_packs_sold: int | None = None
    total_earnings: float | None = None
    account_spend_amount: float | None = None
    traffic_description: str | None = None
    competitive_presence: str | None = None
    
    job_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    location_id: strawberry.ID | None = None
    state_id: strawberry.ID | None = None
    ambassador_id: strawberry.ID | None = None
    
    consumer_engagements: ConsumerEngagementsInput | None = None
    product_samples: List[ProductSampleInput] | None = None
    sales_performance: List[SalesPerformanceInput] | None = None
    consumer_feedback: ConsumerFeedbackInput | None = None
    account_feedback: AccountFeedbackInput | None = None


@strawberry.input
class UpdateRecapInput(CreateRecapInput):
    id: strawberry.ID


@strawberry.input
class DeleteRecapInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class DeleteRecapFileInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class ApproveRecapInput(SparkGraphQLInput):
    id: strawberry.ID
    approved: bool


@strawberry.input
class GenerateRecapPdfInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class ExportRecapsXlsxInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    start_date: str | None = None
    end_date: str | None = None


@strawberry.input
class ExportRecapXlsxInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class RecapFileDownloadUrlInput(SparkGraphQLInput):
    uuid: strawberry.ID
