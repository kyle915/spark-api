import datetime

import strawberry
from strawberry import relay
from asgiref.sync import sync_to_async
from django.utils import timezone

from utils.graphql.permissions import StrictIsAuthenticated
from utils.utils import build_mutation_response
from utils.gcs import extract_blob_name_from_url, delete_blob
from ambassadors.models import Ambassador
from . import models, types, inputs


def _parse_date(raw: str | None) -> datetime.date | None:
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw.strip()[:10])
    except Exception:
        return None


@strawberry.type
class DocumentMobileMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def add_document(
        self,
        info: strawberry.Info,
        input: inputs.AddDocumentInput,
    ) -> types.AddDocumentResponse:
        user = info.context.request.user

        blob_name = extract_blob_name_from_url(input.file)
        if not blob_name:
            return build_mutation_response(
                types.AddDocumentResponse,
                success=False,
                message="Invalid document file path.",
                input_obj=input,
            )

        valid_types = {c.value for c in models.DocumentType}
        doc_type = (input.doc_type or "").strip()
        if doc_type not in valid_types:
            doc_type = models.DocumentType.OTHER.value

        expires_on = _parse_date(input.expires_on)

        @sync_to_async
        def _create():
            ambassador = Ambassador.objects.filter(user=user).first()
            if not ambassador:
                return None
            return models.AmbassadorDocument.objects.create(
                ambassador=ambassador,
                doc_type=doc_type,
                title=(input.title or None),
                file=blob_name,
                original_filename=(input.original_filename or None),
                content_type=(input.content_type or None),
                expires_on=expires_on,
                status=models.DocumentStatus.ACTIVE,
                created_by=user,
                updated_by=user,
            )

        doc = await _create()
        if doc is None:
            return build_mutation_response(
                types.AddDocumentResponse,
                success=False,
                message="No ambassador profile.",
                input_obj=input,
            )
        return build_mutation_response(
            types.AddDocumentResponse,
            success=True,
            message="Document saved.",
            input_obj=input,
            document=doc,
        )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_document(
        self,
        info: strawberry.Info,
        input: inputs.DeleteDocumentInput,
    ) -> types.DeleteDocumentResponse:
        user = info.context.request.user

        @sync_to_async
        def _delete():
            ambassador = Ambassador.objects.filter(user=user).first()
            if not ambassador:
                return None, None
            # uuid-scoped + ownership-scoped — a BA can only delete their own.
            doc = models.AmbassadorDocument.objects.filter(
                uuid=str(input.uuid), ambassador=ambassador
            ).first()
            if not doc:
                return False, None
            blob = None
            try:
                blob = extract_blob_name_from_url(str(doc.file)) if doc.file else None
            except Exception:
                blob = None
            doc.delete()
            return True, blob

        ok, blob = await _delete()
        if ok is None:
            return build_mutation_response(
                types.DeleteDocumentResponse,
                success=False,
                message="No ambassador profile.",
                input_obj=input,
            )
        if not ok:
            return build_mutation_response(
                types.DeleteDocumentResponse,
                success=False,
                message="Document not found.",
                input_obj=input,
            )
        # Best-effort blob cleanup AFTER the row is gone (mirrors update_recap).
        if blob:
            try:
                await sync_to_async(delete_blob)(blob)
            except Exception:
                pass
        return build_mutation_response(
            types.DeleteDocumentResponse,
            success=True,
            message="Document deleted.",
            input_obj=input,
        )
