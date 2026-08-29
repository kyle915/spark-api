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

# Spark-rendered field/custom recap PDFs use these name prefixes. Other
# .pdf attachments (Connecteam source, client uploads) must not count as
# "already generated" or be emailed as the FIELD RECAP.
_SPARK_PDF_NAME_PREFIXES = ("Custom Recap PDF -", "Recap PDF -")


def _is_spark_generated_pdf(pdf_file) -> bool:
    name = (getattr(pdf_file, "name", None) or "").strip()
    if any(name.startswith(prefix) for prefix in _SPARK_PDF_NAME_PREFIXES):
        return True
    blob_val = getattr(pdf_file, "url", None) or getattr(pdf_file, "file", None)
    blob = str(blob_val or "")
    return "recaps/pdfs/" in blob


def _pdf_matches_approval_status(recap, pdf_file) -> bool:
    """True when the stored PDF was rendered with the current approval badge.

    Approve-notify and Generate PDF used to reuse any existing .pdf row.
    Ops often generate a preview while the recap is still a draft; that
    snapshot keeps the red DRAFT chip, then the "recap is ready" email
    attaches it after approval. Require the file to post-date approval.
    """
    if not getattr(recap, "approved", False):
        return True
    approved_at = getattr(recap, "approved_at", None)
    created_at = getattr(pdf_file, "created_at", None)
    if approved_at is not None and created_at is not None:
        return created_at >= approved_at
    # Approved without audit timestamps (legacy) — treat as stale so the
    # APPROVED chip is refreshed on notify / regenerate.
    return False


def _list_spark_generated_pdfs(recap):
    pdf_q = Q(file_type__extension__iexact=".pdf") | Q(
        file_type__extension__iexact="pdf"
    )
    if isinstance(recap, models.CustomRecap):
        qs = recap.custom_recap_files.filter(pdf_q).order_by("-id")
    else:
        qs = recap.recap_files.filter(pdf_q).order_by("-id")
    return [pdf for pdf in qs if _is_spark_generated_pdf(pdf)]


def _delete_spark_generated_pdfs(recap) -> None:
    """Drop prior Spark PDF rows (+ GCS blobs) before writing a fresh render."""
    existing = _list_spark_generated_pdfs(recap)
    if not existing:
        return
    blob_names: list[str] = []
    ids: list[int] = []
    for item in existing:
        blob_val = getattr(item, "url", None) or getattr(item, "file", None)
        blob = extract_blob_name_from_url(str(blob_val)) if blob_val else None
        if blob:
            blob_names.append(blob)
        ids.append(item.id)
    if isinstance(recap, models.CustomRecap):
        models.CustomRecapFile.objects.filter(id__in=ids).delete()
    else:
        models.RecapFile.objects.filter(id__in=ids).delete()
    for blob_name in blob_names:
        try:
            delete_blob(blob_name)
        except Exception:
            logger.warning(
                "Could not delete stale recap PDF blob %s for recap %s",
                blob_name,
                getattr(recap, "id", None),
            )


async def _resolve_recap_pdf_attachment(
    recap: models.Recap | models.CustomRecap,
) -> list[dict] | None:
    """If the recap has a Spark-generated PDF, return Mailer attachments.

    Returns None when no Spark PDF exists or the blob fetch fails —
    caller falls back to a link-only email. Non-Spark .pdf uploads are
    ignored so Connecteam source PDFs are never emailed as the recap.
    """

    def _find_blob() -> tuple[str, str] | None:
        try:
            pdf = _find_existing_pdf_file(recap)
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
    safe_name = (
        friendly_name if friendly_name.lower().endswith(".pdf") else f"{friendly_name}.pdf"
    )
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
    """Most recent Spark-generated PDF on this recap, or None."""
    spark_pdfs = _list_spark_generated_pdfs(recap)
    return spark_pdfs[0] if spark_pdfs else None


def _render_and_store_recap_pdf_sync(recap, user=None, *, force: bool = False):
    """Generate + persist a Spark FIELD RECAP PDF when missing or stale.

    Used by the approve notify path so the email attaches a PDF that
    matches the current approval badge. Reuses an existing Spark PDF only
    when it was rendered after approval (APPROVED chip). Best-effort:
    failures log and return None (link-only email).
    """
    existing = _find_existing_pdf_file(recap)
    if (
        existing
        and not force
        and _pdf_matches_approval_status(recap, existing)
    ):
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
        files = list(
            recap.custom_recap_files.select_related("file_type", "file_recap_category")
        )
        candidates = []
        for recap_file in files:
            if not should_embed_recap_file(recap_file):
                continue
            blob_name = extract_blob_name_from_url(str(recap_file.url))
            if blob_name:
                candidates.append((recap_file, blob_name))
    else:
        files = list(
            recap.recap_files.select_related("file_type", "file_recap_category")
        )
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
        # Drop stale Spark PDFs (e.g. pre-approval DRAFT snapshot) before
        # writing the fresh APPROVED render. Non-Spark .pdf attachments stay.
        if existing or force:
            _delete_spark_generated_pdfs(recap)

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
    """Ensure the approve email can attach an APPROVED-badge PDF.

    Always refresh when the stored Spark PDF predates approval (or is
    missing). Passing force=False still regenerates stale DRAFT snapshots
    via ``_pdf_matches_approval_status``.
    """
    await sync_to_async(_render_and_store_recap_pdf_sync)(recap)
