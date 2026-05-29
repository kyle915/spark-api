from __future__ import annotations

import strawberry
import strawberry_django
from strawberry.relay import Node
from asgiref.sync import sync_to_async

from utils.gcs import public_url, extract_blob_name_from_url
from . import models


@strawberry_django.type(models.AmbassadorDocument)
class AmbassadorDocument(Node):
    uuid: str
    doc_type: str
    title: str | None
    original_filename: str | None
    content_type: str | None
    expires_on: str | None  # ISO date "YYYY-MM-DD" or null
    status: str
    created_at: str
    updated_at: str

    # Mirror recaps/types.py RecapFile.file_url: a differently-named
    # Python resolver aliased to the GraphQL field `file`, so it does not
    # shadow the Django FileField (which would force a per-row DB reload).
    @strawberry.field(name="file")
    async def file_url(self) -> str | None:
        field_file = self.__dict__.get("file")
        if field_file is None:
            def _reload():
                self.refresh_from_db(fields=["file"])
                return self.__dict__.get("file")
            try:
                field_file = await sync_to_async(
                    _reload, thread_sensitive=True
                )()
            except Exception:
                return None
        if not field_file:
            return None
        try:
            blob = field_file.name
        except Exception:
            blob = str(field_file)
        blob_name = extract_blob_name_from_url(blob)
        return public_url(blob_name)

    # Convenience flags the mobile client uses for the expiry chip.
    @strawberry.field
    def is_expired(self) -> bool:
        from django.utils import timezone
        if not self.expires_on:
            return False
        try:
            return self.expires_on < timezone.now().date()
        except Exception:
            return False

    @strawberry.field
    def days_until_expiry(self) -> int | None:
        from django.utils import timezone
        if not self.expires_on:
            return None
        try:
            return (self.expires_on - timezone.now().date()).days
        except Exception:
            return None


@strawberry.type
class AddDocumentResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    document: AmbassadorDocument | None = None


@strawberry.type
class DeleteDocumentResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
