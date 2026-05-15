import strawberry
from typing import List
from strawberry.scalars import JSON

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class RecapFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    event_id: strawberry.ID | None = None
    event_type: strawberry.ID | None = None
    rmm_asigned_id: strawberry.ID | None = None
    ambassador_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    location_id: strawberry.ID | None = None
    state_id: strawberry.ID | None = None
    event_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    event_address: str | None = None
    approved: bool | None = None
    edited: bool | None = None


@strawberry.input
class CustomRecapFiltersInput(RecapFiltersInput):
    custom_recap_template_id: strawberry.ID | None = None


@strawberry.input
class CustomRecapTemplateFiltersInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    event_type_id: strawberry.ID | None = None


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
    total_consumer: int | None = None
    first_time_consumers: int | None = None
    brand_aware_consumers: int | None = None
    willing_to_purchase_consumers: int | None = None
    not_willing_consumers: int | None = None


@strawberry.input
class ProductSampleInput(SparkGraphQLInput):
    product_id: strawberry.ID | None = None
    quantity: int | None = None


@strawberry.input
class SalesPerformanceInput(SparkGraphQLInput):
    product_id: strawberry.ID | None = None
    type_of_good_id: strawberry.ID | None = None
    price: float | None = None


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
class ApproveCustomRecapInput(SparkGraphQLInput):
    id: strawberry.ID
    approved: bool


@strawberry.input
class DeclineCustomRecapInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class GenerateRecapPdfInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class GenerateCustomRecapPdfInput(SparkGraphQLInput):
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
class ExportCustomRecapsXlsxInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None
    custom_recap_template_id: strawberry.ID | None = None
    start_date: str | None = None
    end_date: str | None = None


@strawberry.input
class ExportCustomRecapXlsxInput(SparkGraphQLInput):
    id: strawberry.ID


@strawberry.input
class RecapFileDownloadUrlInput(SparkGraphQLInput):
    uuid: strawberry.ID


@strawberry.input
class CustomFieldValueInput(SparkGraphQLInput):
    custom_field_id: strawberry.ID | None = None
    custom_field_value_id: strawberry.ID | None = None
    value: str


@strawberry.input
class CreateCustomRecapInput(SparkGraphQLInput):
    name: str
    event_id: strawberry.ID
    custom_recap_template_id: strawberry.ID

    files: List[RecapFileInput] | None = None
    product_samples: List[ProductSampleInput] | None = None
    sales_performance: List[SalesPerformanceInput] | None = None

    total_engagements: int | None = None
    filling_for_ambassador: bool | None = None
    late: bool | None = None
    incomplete: bool | None = None
    approved: bool | None = None
    used_corpo_card: bool | None = None

    job_id: strawberry.ID | None = None
    retailer_id: strawberry.ID | None = None
    location_id: strawberry.ID | None = None
    state_id: strawberry.ID | None = None
    ambassador_id: strawberry.ID | None = None
    custom_field_values: List[CustomFieldValueInput] | None = None


@strawberry.input
class CreateCustomRecapMobileInput(SparkGraphQLInput):
    name: str
    job_id: strawberry.ID
    custom_recap_template_id: strawberry.ID

    files: List[RecapFileInput] | None = None
    product_samples: List[ProductSampleInput] | None = None
    sales_performance: List[SalesPerformanceInput] | None = None

    total_engagements: int | None = None
    filling_for_ambassador: bool | None = None
    late: bool | None = None
    incomplete: bool | None = None
    approved: bool | None = None
    used_corpo_card: bool | None = None

    custom_field_values: List[CustomFieldValueInput] | None = None


@strawberry.input
class UpdateCustomRecapInput(CreateCustomRecapInput):
    id: strawberry.ID


@strawberry.input
class UpdateCustomRecapMobileFilesInput(SparkGraphQLInput):
    add: List[RecapFileInput] | None = None
    remove: List[strawberry.ID] | None = None


@strawberry.input
class UpdateCustomRecapMobileInput(SparkGraphQLInput):
    id: strawberry.ID
    name: str

    files: UpdateCustomRecapMobileFilesInput | None = None
    product_samples: List[ProductSampleInput] | None = None
    sales_performance: List[SalesPerformanceInput] | None = None

    total_engagements: int | None = None
    filling_for_ambassador: bool | None = None
    late: bool | None = None
    incomplete: bool | None = None
    approved: bool | None = None
    used_corpo_card: bool | None = None

    custom_field_values: List[CustomFieldValueInput] | None = None


@strawberry.input
class CreateCustomFieldInput(SparkGraphQLInput):
    name: str
    custom_recap_template_id: strawberry.ID
    custom_field_type_id: strawberry.ID
    recap_section_id: strawberry.ID
    required: bool | None = None


@strawberry.input
class UpdateCustomFieldInput(CreateCustomFieldInput):
    id: strawberry.ID


@strawberry.input
class CustomRecapTemplateFieldInput(SparkGraphQLInput):
    name: str
    custom_field_type_id: strawberry.ID
    recap_section_id: strawberry.ID
    id: strawberry.ID | None = None
    required: bool | None = None


@strawberry.input
class CreateCustomRecapTemplateInput(SparkGraphQLInput):
    name: str
    event_type_id: strawberry.ID
    product_samples: bool | None = None
    sales_performance: bool | None = None
    layout: JSON | None = None
    custom_fields: List[CustomRecapTemplateFieldInput] | None = None


@strawberry.input
class UpdateCustomRecapTemplateInput(CreateCustomRecapTemplateInput):
    id: strawberry.ID


@strawberry.input
class CreateCustomRecapFieldTypeInput(SparkGraphQLInput):
    name: str


@strawberry.input
class UpdateCustomRecapFieldTypeInput(CreateCustomRecapFieldTypeInput):
    id: strawberry.ID


@strawberry.input
class CreateRecapSectionInput(SparkGraphQLInput):
    name: str
    tenant_id: strawberry.ID


@strawberry.input
class UpdateRecapSectionInput(CreateRecapSectionInput):
    id: strawberry.ID
