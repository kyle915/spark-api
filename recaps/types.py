import strawberry_django
import strawberry
from typing import List

from events import types as event_types
from ambassadors import types as ambassador_types
from . import models
from utils.gcs import generate_download_url, extract_blob_name_from_url


@strawberry_django.type(models.RecapFile)
class RecapFile:
    id: strawberry.ID
    uuid: str
    name: str
    approved: bool
    file_type_id: strawberry.ID
    created_at: str
    updated_at: str

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


@strawberry_django.type(models.Recap)
class Recap:
    id: strawberry.ID
    uuid: str
    name: str
    event: event_types.Event
    event_id: strawberry.ID
    recap_file: RecapFile
    recap_file_id: strawberry.ID
    created_at: str
    updated_at: str

    @strawberry.field
    def recap_files(self) -> List[RecapFile]:
        """Return all recap files linked to this recap."""
        return [relation.recap_file for relation in self.recap_recap_file.all()]

    @strawberry.field
    def ambassadors(self) -> List[ambassador_types.Ambassador]:
        """Return ambassadors linked to the recap's event."""
        return [ae.ambassador for ae in self.event.ambassadors_events.all()]

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


@strawberry_django.type(models.RecapRecapFile)
class RecapRecapFile:
    id: strawberry.ID
    uuid: str
    recap_file_id: strawberry.ID
    recap_id: strawberry.ID
    created_at: str
    updated_at: str
