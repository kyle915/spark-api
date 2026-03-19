from __future__ import annotations

import strawberry_django
import strawberry
from strawberry.relay import Node
from typing import List

from events import types as event_types
from jobs import types as job_types
from ambassadors import types as ambassador_types
from . import models
from utils.gcs import generate_download_url, extract_blob_name_from_url


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

    @strawberry.field
    def file(self) -> str | None:
        """Return a signed URL for the product image if it exists."""
        if not self.file:
            return None
        blob_name = extract_blob_name_from_url(self.file.name)
        if not blob_name:
            return None
        return generate_download_url(blob_name)


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
    total_consumer: int
    first_time_consumers: int
    brand_aware_consumers: int
    willing_to_purchase_consumers: int
    not_willing_consumers: int
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

    @strawberry.field
    def recap_file(self) -> RecapFile | None:
        """Return first linked recap file for backward compatibility."""
        files = list(self.recap_files.all())
        return files[0] if files else None

    @strawberry.field
    def recap_file_id(self) -> strawberry.ID | None:
        """Return id for the first linked recap file."""
        files = list(self.recap_files.all())
        return strawberry.ID(str(files[0].id)) if files else None

    @strawberry.field
    def recap_files(self) -> List[RecapFile]:
        """Return all recap files linked to this recap."""
        return list(self.recap_files.all())

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


@strawberry.type
class RecapListResponse:
    total_pages: int
    recaps: List[Recap]
