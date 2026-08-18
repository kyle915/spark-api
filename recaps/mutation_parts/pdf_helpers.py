"""PDF attach / render helpers used by approve-notify and generate*Pdf."""
import base64
import logging

from asgiref.sync import sync_to_async
from django.db.models import Q
from django.utils import timezone as django_timezone

from ambassadors.models import FileType
from recaps import models

logger = logging.getLogger("recaps.mutations")
from recaps.pdf import (
    build_recap_pdf,
    should_embed_recap_file,
    is_image_bytes,
    downscale_image_bytes,
    IMAGE_EXTENSIONS,
)
from utils.gcs import (
    public_url,
    extract_blob_name_from_url,
    delete_blob,
    upload_bytes,
    download_blob_bytes,
    generate_download_url,
    get_gcs_client,
)

async def _resolve_recap_pdf_attachment(
    recap: models.Recap | models.CustomRecap,
) -> list[dict] | None:
    """If the recap has a generated PDF (CustomRecapFile with .pdf
    extension or RecapFile equivalent), return an `attachments` list
    shaped for the Mailer. Returns None when no PDF exists or the
    blob fetch fails — caller falls back to a link-only email.
    """
    def _find_blob() -> tuple[str, str] | None:
        try:
            if isinstance(recap, models.CustomRecap):
                qs = recap.custom_recap_files.filter(
                    file_type__extension__iexact=".pdf"
                ) | recap.custom_recap_files.filter(
                    file_type__extension__iexact="pdf"
                )
                pdf = qs.order_by("-id").first()
            else:
                qs = recap.recap_files.filter(
                    file_type__extension__iexact=".pdf"
                ) | recap.recap_files.filter(
                    file_type__extension__iexact="pdf"
                )
                pdf = qs.order_by("-id").first()
            if not pdf:
                return None
            # RecapFile stores the blob on ``file``; CustomRecapFile on ``url``.
            blob_val = getattr(pdf, "url", None) or getattr(pdf, "file", None)
            blob = extract_blob_name_from_url(str(blob_val)) or str(blob_val)
            return blob, (pdf.name or f"recap-{recap.uuid}.pdf")
        except Exception:
            return None

    found = await sync_to_async(_find_blob)()
    if not found:
        return None
    blob_name, friendly_name = found
    try:
        pdf_bytes = await sync_to_async(download_blob_bytes)(blob_name)
    except Exception as exc:
        logger.warning(
            "Could not fetch PDF blob %s for recap %s: %s",
            blob_name,
            recap.id,
            exc,
        )
        return None
    if not pdf_bytes:
        return None
    safe_name = friendly_name if friendly_name.lower().endswith(".pdf") else f"{friendly_name}.pdf"
    # Resend's Python SDK JSON-encodes the send payload. Raw bytes raise
    # ``Object of type bytes is not JSON serializable`` and the request
    # never leaves Cloud Run. Campaign / monthly report mailers already
    # send base64; keep this path on the same contract.
    return [
        {
            "filename": safe_name,
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
            "content_type": "application/pdf",
        }
    ]


def _find_existing_pdf_file(recap):
    """Most recent PDF file row on this recap, or None."""
    pdf_q = Q(file_type__extension__iexact=".pdf") | Q(file_type__extension__iexact="pdf")
    if isinstance(recap, models.CustomRecap):
        return recap.custom_recap_files.filter(pdf_q).order_by("-id").first()
    return recap.recap_files.filter(pdf_q).order_by("-id").first()


def _render_and_store_recap_pdf_sync(recap, user=None):
    """Generate + persist a recap PDF if one is not already stored.

    Used by the approve notify path so the email can attach the PDF
    without regenerating on every approve. Best-effort: failures log
    and return None (link-only email).
    """
    existing = _find_existing_pdf_file(recap)
    if existing:
        return existing
    actor = user or getattr(recap, "updated_by", None) or getattr(recap, "created_by", None)
    if actor is None:
        return None
    file_type = FileType.objects.filter(
        Q(extension__iexact=".pdf") | Q(extension__iexact="pdf")
    ).first()
    if not file_type:
        return None

    import concurrent.futures as _cf

    image_entries = []
    if isinstance(recap, models.CustomRecap):
        files = list(recap.custom_recap_files.select_related("file_type", "file_recap_category"))
        candidates = []
        for recap_file in files:
            if not should_embed_recap_file(recap_file):
                continue
            blob_name = extract_blob_name_from_url(str(recap_file.url))
            if blob_name:
                candidates.append((recap_file, blob_name))
    else:
        files = list(recap.recap_files.select_related("file_type", "file_recap_category"))
        candidates = []
        for recap_file in files:
            if not should_embed_recap_file(recap_file):
                continue
            blob_name = extract_blob_name_from_url(str(recap_file.file))
            if blob_name:
                candidates.append((recap_file, blob_name))

    def _fetch_one(item):
        recap_file, blob_name = item
        try:
            data = download_blob_bytes(blob_name)
        except Exception:
            return None
        if not data or not is_image_bytes(data):
            return None
        data = downscale_image_bytes(data) or data
        return {
            "name": recap_file.name,
            "bytes": data,
            "category": (
                recap_file.file_recap_category.name
                if recap_file.file_recap_category
                else "Uncategorized"
            ),
        }

    if candidates:
        with _cf.ThreadPoolExecutor(max_workers=16) as pool:
            for entry in pool.map(_fetch_one, candidates):
                if entry is not None:
                    image_entries.append(entry)

    try:
        pdf_bytes = build_recap_pdf(recap, image_entries)
        timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
        if isinstance(recap, models.CustomRecap):
            blob_name = f"recaps/pdfs/custom-{recap.uuid}-{timestamp}.pdf"
            upload_bytes(blob_name, pdf_bytes, content_type="application/pdf")
            return models.CustomRecapFile.objects.create(
                name=f"Custom Recap PDF - {recap.name}",
                url=blob_name,
                file_type=file_type,
                custom_recap=recap,
                approved=False,
                created_by=actor,
            )
        blob_name = f"recaps/pdfs/{recap.uuid}-{timestamp}.pdf"
        upload_bytes(blob_name, pdf_bytes, content_type="application/pdf")
        return models.RecapFile.objects.create(
            name=f"Recap PDF - {recap.name}",
            file=blob_name,
            file_type=file_type,
            recap=recap,
            approved=False,
            created_by=actor,
        )
    except Exception:
        logger.exception("recap PDF store failed recap=%s", getattr(recap, "id", None))
        return None


async def _ensure_recap_pdf_for_notify(recap) -> None:
    await sync_to_async(_render_and_store_recap_pdf_sync)(recap)
