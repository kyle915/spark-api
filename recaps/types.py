from __future__ import annotations

import strawberry_django
import strawberry
from strawberry.relay import Node
from typing import List
from strawberry.scalars import JSON

from events import types as event_types
from jobs import types as job_types
from ambassadors import types as ambassador_types
from tenants import types as tenant_types
from . import models
from utils.gcs import public_url, extract_blob_name_from_url


@strawberry_django.type(models.RecapFile)
class RecapFile(Node):
    uuid: str
    name: str
    approved: bool
    file_type_id: strawberry.ID
    file_recap_category_id: strawberry.ID | None
    created_at: str
    updated_at: str

    file_type: ambassador_types.FileType
    file_recap_category: FileRecapCategory | None

    @strawberry_django.field(only=["file"])
    def file(self) -> str | None:
        """Return the public URL for the recap file if one exists.

        Re-read via the model manager because this resolver shadows
        the FileField named `file`. Returns the unsigned bucket URL
        so it loads cross-origin without a private key.
        """
        from .models import RecapFile as RecapFileModel
        row = RecapFileModel.objects.only("id", "file").get(pk=self.pk)
        if not row.file:
            return None
        blob_name = extract_blob_name_from_url(row.file.name)
        return public_url(blob_name)


@strawberry.type
class RecapFileDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    recap_file: RecapFile | None = None


@strawberry.type
class RecapExportResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    file_url: str | None = None


@strawberry.type
class RecapFileUrlResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    file_url: str | None = None


@strawberry_django.type(models.ConsumerEngagements)
class ConsumerEngagements(Node):
    uuid: str
    total_consumer: int | None
    first_time_consumers: int | None
    brand_aware_consumers: int | None
    willing_to_purchase_consumers: int | None
    not_willing_consumers: int | None
    created_at: str
    updated_at: str


@strawberry_django.type(models.ProductSamples)
class ProductSamples(Node):
    uuid: str
    product: event_types.Product
    quantity: int
    created_at: str
    updated_at: str


@strawberry_django.type(models.TypeOfGood)
class TypeOfGood(Node):
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry_django.type(models.FileRecapCategory)
class FileRecapCategory(Node):
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry_django.type(models.SalesPerformance)
class SalesPerformance(Node):
    uuid: str
    product: event_types.Product
    type_of_good: TypeOfGood
    price: float
    created_at: str
    updated_at: str


@strawberry_django.type(models.ConsumerFeedback)
class ConsumerFeedback(Node):
    uuid: str
    demographics: str | None
    feedback: str | None
    quotes: str | None
    positive_stories: str | None
    reasons_to_decline: str | None
    created_at: str
    updated_at: str


@strawberry_django.type(models.AccountFeedback)
class AccountFeedback(Node):
    uuid: str
    do_differently_feedback: str | None
    feedback: str | None
    corpo_card: str | None
    was_corpo_card_used: bool
    created_at: str
    updated_at: str


@strawberry_django.type(models.Recap)
class Recap(Node):
    uuid: str
    name: str
    approved: bool
    filling_for_ambassador: bool
    event: event_types.Event
    event_id: strawberry.ID
    ambassador: ambassador_types.Ambassador | None
    job_id: strawberry.ID | None
    job: job_types.Job | None
    retailer_id: strawberry.ID | None
    retailer: event_types.Retailer | None
    location_id: strawberry.ID | None
    location: event_types.Location | None
    state_id: strawberry.ID | None
    state: event_types.State | None
    created_at: str
    updated_at: str

    total_engagements: int | None
    products_sold: int | None
    total_cans_sold: int | None
    total_packs_sold: int | None
    total_earnings: float | None
    account_spend_amount: float | None
    traffic_description: str | None
    competitive_presence: str | None

    # Relationships
    consumer_engagements: List[ConsumerEngagements]
    product_samples: List[ProductSamples]
    sales_performance: List[SalesPerformance]
    consumer_feedback: List[ConsumerFeedback]
    account_feedback: List[AccountFeedback]

    # NOTE: we use models.RecapFile.objects.filter(recap=self) instead
    # of self.recap_files.all() because defining a method named
    # `recap_files` on a strawberry type shadows the Django reverse-
    # accessor of the same name. Inside the resolver body, `self.recap_files`
    # would resolve to the bound method (not the manager), and `.all()`
    # would silently fail — returning [] for every recap in the API
    # despite the DB having the rows. Going through the model manager
    # directly side-steps the name collision.

    @strawberry.field
    def recap_file(self) -> RecapFile | None:
        """Return first linked recap file for backward compatibility."""
        first = (
            models.RecapFile.objects.filter(recap=self)
            .order_by("id")
            .first()
        )
        return first

    @strawberry.field
    def recap_file_id(self) -> strawberry.ID | None:
        """Return id for the first linked recap file."""
        first = (
            models.RecapFile.objects.filter(recap=self)
            .order_by("id")
            .first()
        )
        return strawberry.ID(str(first.id)) if first else None

    @strawberry.field
    def recap_files(self) -> List[RecapFile]:
        """Return all recap files linked to this recap."""
        return list(
            models.RecapFile.objects.filter(recap=self).order_by("id")
        )

    @strawberry.field(deprecation_reason="Use ambassador instead.")
    def ambassadors(self) -> List[ambassador_types.Ambassador]:
        """Backward-compatible ambassador list wrapper."""
        return [self.ambassador] if self.ambassador else []

    @strawberry.field
    def request_store_managers(self) -> List[event_types.RequestStoreManager]:
        """Return store managers associated with the recap's request."""
        if not self.event or not self.event.request:
            return []
        return list(self.event.request.requests_stores_manager.all())


@strawberry.type
class RecapDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    recap: Recap | None = None


@strawberry_django.type(models.CustomRecap)
class CustomRecap(Node):
    uuid: str
    name: str
    submitted_at: str | None
    total_engagements: int | None
    filling_for_ambassador: bool
    late: bool
    incomplete: bool
    approved: bool
    used_corpo_card: bool
    timezone_id: strawberry.ID | None

    event_id: strawberry.ID
    event: event_types.Event
    ambassador_id: strawberry.ID | None
    ambassador: ambassador_types.Ambassador | None
    job_id: strawberry.ID | None
    job: job_types.Job | None
    retailer_id: strawberry.ID | None
    retailer: event_types.Retailer | None
    location_id: strawberry.ID | None
    location: event_types.Location | None
    state_id: strawberry.ID | None
    state: event_types.State | None
    custom_recap_template_id: strawberry.ID
    custom_recap_template: "CustomRecapTemplate"
    created_at: str
    updated_at: str
    custom_recap_product_sample: List["CustomRecapProductSample"]
    custom_recap_sale_performance: List["CustomRecapSalePerformance"]
    custom_recap_files: List["CustomRecapFile"]

    @strawberry.field
    def custom_field(self) -> List["CustomField"]:
        """
        Return template fields enriched with the value submitted in this custom recap.
        Value source: CustomFieldValue table.
        """
        value_by_field_id = {
            item.custom_field_id: item for item in self.custom_field_value.all()
        }
        fields: list[CustomField] = []
        for custom_field in self.custom_recap_template.custom_field.all():
            custom_field_value = value_by_field_id.get(custom_field.id)
            setattr(
                custom_field,
                "_custom_recap_value",
                custom_field_value.value if custom_field_value else None,
            )
            setattr(
                custom_field,
                "_custom_field_value_id",
                custom_field_value.id if custom_field_value else None,
            )
            fields.append(custom_field)
        return fields


@strawberry_django.type(models.CustomFieldValue)
class CustomFieldValue(Node):
    uuid: str
    value: str
    custom_field_id: strawberry.ID
    custom_field: "CustomField"
    created_at: str
    updated_at: str


@strawberry_django.type(models.CustomRecapProductSample)
class CustomRecapProductSample(Node):
    uuid: str
    product: event_types.Product
    quantity: int
    created_at: str
    updated_at: str


@strawberry_django.type(models.CustomRecapSalePerformance)
class CustomRecapSalePerformance(Node):
    uuid: str
    product: event_types.Product
    type_of_good: TypeOfGood
    price: float
    created_at: str
    updated_at: str


@strawberry_django.type(models.CustomRecapFile)
class CustomRecapFile(Node):
    uuid: str
    name: str
    approved: bool
    file_type_id: strawberry.ID
    file_recap_category_id: strawberry.ID | None
    created_at: str
    updated_at: str

    file_type: ambassador_types.FileType
    file_recap_category: FileRecapCategory | None

    @strawberry_django.field(only=["url"])
    def url(self) -> str | None:
        """Return the public URL for the custom recap file if any."""
        from .models import CustomRecapFile as CRF
        row = CRF.objects.only("id", "url").get(pk=self.pk)
        if not row.url:
            return None
        blob_name = extract_blob_name_from_url(row.url.name)
        return public_url(blob_name)


@strawberry.type
class CustomRecapDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_recap: CustomRecap | None = None


@strawberry.type
class CustomRecapFileDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_recap_file: CustomRecapFile | None = None


@strawberry_django.type(models.CustomField)
class CustomField(Node):
    uuid: str
    name: str
    required: bool
    custom_recap_template_id: strawberry.ID
    custom_field_type_id: strawberry.ID
    custom_field_type: "CustomRecapFieldType"
    recap_section_id: strawberry.ID
    recap_section: "RecapSection"
    created_at: str
    updated_at: str

    @strawberry.field
    def value(self) -> str | None:
        """Value for this field in a specific custom recap context, if present."""
        return getattr(self, "_custom_recap_value", None)

    @strawberry.field
    def custom_field_value_id(self) -> strawberry.ID | None:
        """Custom field value ID for this recap context, if present."""
        return getattr(self, "_custom_field_value_id", None)


@strawberry.type
class CustomFieldDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_field: CustomField | None = None


@strawberry_django.type(models.CustomRecapTemplate)
class CustomRecapTemplate(Node):
    uuid: str
    name: str
    product_samples: bool
    sales_performance: bool
    layout: JSON
    event_type_id: strawberry.ID
    event_type: event_types.EventType
    tenant_id: strawberry.ID
    tenant: tenant_types.TenantType
    created_at: str
    updated_at: str
    custom_field: List[CustomField]


@strawberry.type
class CustomRecapTemplateDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_recap_template: CustomRecapTemplate | None = None


@strawberry_django.type(models.CustomRecapFieldType)
class CustomRecapFieldType(Node):
    uuid: str
    name: str
    created_at: str
    updated_at: str


@strawberry.type
class CustomRecapFieldTypeDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    custom_recap_field_type: CustomRecapFieldType | None = None


@strawberry_django.type(models.RecapSection)
class RecapSection(Node):
    uuid: str
    name: str
    tenant_id: strawberry.ID
    created_at: str
    updated_at: str


@strawberry.type
class RecapSectionDetailResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    recap_section: RecapSection | None = None


@strawberry.type
class RecapListResponse:
    total_pages: int
    recaps: List[Recap]
