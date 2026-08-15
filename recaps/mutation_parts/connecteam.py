"""Connecteam PDF import — GCS upload + enqueue, not 100MB GraphQL base64."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone as django_timezone
from graphql import GraphQLError

from ambassadors.models import FileType
from events.models import Event
from recaps import models
from recaps import types
from recaps.mutation_parts.file_categories import _resolve_file_recap_category
from utils.cloud_tasks import enqueue, enqueue_or_background
from utils.gcs import download_blob_bytes, upload_bytes
from utils.graphql.mixins import resolve_id_to_int
from utils.utils import build_mutation_response

User = get_user_model()
logger = logging.getLogger("recaps.mutations")

JOB_PREFIX = "connecteam-imports/jobs/"


def _job_blob(job_id: str) -> str:
    return f"{JOB_PREFIX}{job_id}.json"


def write_connecteam_job(job_id: str, payload: dict) -> None:
    upload_bytes(
        _job_blob(job_id),
        json.dumps(payload).encode("utf-8"),
        content_type="application/json",
    )


def read_connecteam_job(job_id: str) -> dict | None:
    try:
        raw = download_blob_bytes(_job_blob(job_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


async def execute_connecteam_import_from_bytes(
    *,
    user,
    event,
    template,
    pdf_bytes: bytes,
    name: str | None,
    input_obj=None,
):
    """Parse PDF bytes and draft a CustomRecap. Shared by sync + task paths."""
    import base64  # kept so patches on recaps.mutations still work if rebound

    from recaps.connecteam import (
        parse_pdf_bytes,
        match_fields,
        route_single_label_images,
        is_receipt_label,
    )
    from strawberry import ID
    import strawberry

    try:
        parsed = await sync_to_async(parse_pdf_bytes)(pdf_bytes)
    except Exception as e:
        logging.getLogger(__name__).exception(
            "connecteam-import: PDF parse failed event_id=%s", event.id,
        )
        return build_mutation_response(
            types.ImportConnecteamRecapPdfResponse,
            success=False,
            message=f"Couldn't read PDF: {e}",
            input_obj=input_obj,
        )

    if not parsed.raw_pairs:
        # Diagnostic: show the first ~200 chars of what pypdf
        # actually extracted, so the admin (and we, debugging
        # later) can tell whether the PDF was empty, image-only,
        # or just a layout the parser doesn't know yet.
        total_text = "\n".join(parsed.page_texts)
        preview = total_text[:200].replace("\n", " ⏎ ").strip()
        text_len = len(total_text)
        page_count = len(parsed.page_texts)
        image_count = len(parsed.images)
        return build_mutation_response(
            types.ImportConnecteamRecapPdfResponse,
            success=False,
            message=(
                f"No labeled fields found in PDF "
                f"(pages={page_count}, text={text_len}c, "
                f"images={image_count}). The parser looks for "
                f"'Label::' or 'Label:' pairs. Extracted text "
                f"started with: {preview!r}"
            ),
            input_obj=input_obj,
        )

    custom_fields = await sync_to_async(list)(
        models.CustomField.objects.filter(custom_recap_template=template)
        .select_related("custom_field_type", "recap_section")
    )

    match_results = match_fields(parsed, custom_fields)

    # Default name → the event's OWN name (+ its date) so a recap list
    # full of Connecteam imports isn't a wall of identical "Imported
    # from Connecteam · <today>" rows (Kyle's report: every import was
    # named the same, which is messy in the recap list). The stamp comes
    # from the EVENT date — not today — so two same-named stores on
    # different days stay distinguishable. An explicit input.name (the
    # import modal's "Recap title" field) always wins; the generic
    # "Imported from Connecteam" stamp is only the last resort for an
    # event with no name.
    name = (input.name or "").strip()
    if not name:
        ev_name = (getattr(event, "name", "") or "").strip()
        ev_date = getattr(event, "date", None)
        stamp = (
            ev_date.date().isoformat()
            if ev_date
            else django_timezone.now().strftime("%Y-%m-%d")
        )
        name = (
            f"{ev_name} · {stamp}"
            if ev_name
            else f"Imported from Connecteam · {stamp}"
        )

    def _create() -> tuple[models.CustomRecap, int]:
        from django.core.files.base import ContentFile

        # Count of embedded PDF photos actually attached — returned so
        # the frontend can send the admin to upload photos when the PDF
        # carried none.
        images_attached = 0

        with transaction.atomic():
            recap = models.CustomRecap.objects.create(
                name=name,
                event=event,
                tenant=event.tenant,
                custom_recap_template=template,
                created_by=user,
                submitted_at=django_timezone.now(),
            )
            for mr in match_results:
                if mr.field_id is None:
                    continue
                if not mr.pdf_value:
                    continue
                models.CustomFieldValue.objects.create(
                    custom_recap=recap,
                    custom_field_id=mr.field_id,
                    value=mr.pdf_value,
                    created_by=user,
                )

            # Stash the source PDF as a CustomRecapFile so the
            # admin can audit / re-download the original from the
            # recap view. Without this, the PDF the user uploaded
            # is gone the moment the mutation responds — only the
            # extracted values remain.
            #
            # File-recap-category is intentionally NOT set —
            # Kyle's team wants imported files to render as one
            # flat gallery, not grouped by category (PR #543
            # added grouping; this reverts that on Kyle's
            # explicit ask).
            try:
                pdf_filetype, _ = FileType.objects.get_or_create(
                    name="pdf",
                )
                source_file = models.CustomRecapFile(
                    custom_recap=recap,
                    file_type=pdf_filetype,
                    name="Connecteam source PDF",
                    approved=False,
                    created_by=user,
                )
                source_file.url.save(
                    f"connecteam-source-{recap.uuid}.pdf",
                    ContentFile(pdf_bytes),
                    save=False,
                )
                source_file.save()
            except Exception:
                # Non-fatal — the recap itself was created
                # successfully. Audit-trail file is nice-to-have.
                logging.getLogger(__name__).exception(
                    "connecteam-import: source PDF attach failed "
                    "recap_id=%s", recap.id,
                )

            # Extract embedded images from the PDF (sampling photos,
            # table-setup pics, in-stock product, receipt, etc.)
            # and attach each as a CustomRecapFile. Without this
            # step, Kyle's team has to manually re-upload every
            # photo even after a successful field-text import.
            #
            # Kyle's call: imported photos render as one flat gallery —
            # NO FileRecapCategory tagging — EXCEPT the receipt, which
            # groups under the tenant's "Receipts" category so it lands in
            # "Evidences & Attachments" under a Receipts group (like a
            # native recap). The preceding-label hint drives both the
            # per-file `name` and the receipt detection.
            try:
                image_filetype, _ = FileType.objects.get_or_create(
                    name="image",
                )
                # Tenant "Receipts" category (sentinel "2"); get-or-create,
                # tenant-scoped. None-safe — a failure just leaves the
                # receipt uncategorized rather than blocking the import.
                receipts_category = _resolve_file_recap_category(
                    "2", tenant_id=event.tenant_id,
                )
                attached_images: list = []
                for parsed_img in parsed.images:
                    # Skip obvious zero-byte / placeholder entries.
                    if not parsed_img.bytes_:
                        continue
                    if len(parsed_img.bytes_) < 1024:
                        # Sub-1KB blobs are almost always icons,
                        # logos, or rendering artifacts — not the
                        # full-size sampling photos we want.
                        continue
                    # Name carries the preceding-label hint so the
                    # admin can tell receipt from sampling photo
                    # at a glance, even though the gallery is flat.
                    nice_name = (
                        parsed_img.preceding_label
                        or f"PDF page {parsed_img.page_index + 1}"
                    )
                    is_receipt = is_receipt_label(
                        parsed_img.preceding_label
                    )
                    file_row = models.CustomRecapFile(
                        custom_recap=recap,
                        file_type=image_filetype,
                        name=nice_name,
                        approved=False,
                        created_by=user,
                        file_recap_category=(
                            receipts_category if is_receipt else None
                        ),
                    )
                    file_row.url.save(
                        (
                            f"connecteam-img-{recap.uuid}"
                            f"-p{parsed_img.page_index}"
                            f"-i{parsed_img.image_index}"
                            f"{parsed_img.extension}"
                        ),
                        ContentFile(parsed_img.bytes_),
                        save=False,
                    )
                    file_row.save()
                    images_attached += 1
                    # Receipts live in Evidences under "Receipts" — NOT
                    # also routed onto the receipt field (Kyle picked
                    # Evidences over the dedicated field). Only non-receipt
                    # images are eligible for single-label field routing.
                    if not is_receipt:
                        attached_images.append(
                            (parsed_img, file_row.url.name)
                        )

                # Route a single, unambiguously-labeled image (the
                # receipt) onto its IMAGE field's VALUE so it renders in
                # place, not just the flat gallery. Narrow by design
                # (exactly-one exact-label match — see
                # route_single_label_images), so multi-image sampling /
                # table photos stay flat. The image stays in the gallery
                # too; this only ALSO sets the field value.
                image_fields = [
                    cf
                    for cf in custom_fields
                    if getattr(cf.custom_field_type, "name", "") == "image"
                ]
                for fid, blob in route_single_label_images(
                    attached_images, image_fields
                ).items():
                    models.CustomFieldValue.objects.get_or_create(
                        custom_recap=recap,
                        custom_field_id=fid,
                        defaults={"value": blob, "created_by": user},
                    )
            except Exception:
                logging.getLogger(__name__).exception(
                    "connecteam-import: image attach failed "
                    "recap_id=%s", recap.id,
                )

            return recap, images_attached

    try:
        recap, images_attached = await sync_to_async(_create)()
    except Exception as e:
        logging.getLogger(__name__).exception(
            "connecteam-import: DB write failed event_id=%s", event.id,
        )
        return build_mutation_response(
            types.ImportConnecteamRecapPdfResponse,
            success=False,
            message=f"Couldn't create draft recap: {e}",
            input_obj=input_obj,
        )

    matched = sum(1 for mr in match_results if mr.field_id and mr.pdf_value)
    unmatched = sum(1 for mr in match_results if not mr.field_id)
    stats = [
        types.ImportConnecteamRecapPdfStat(
            pdf_label=mr.pdf_label,
            pdf_value=mr.pdf_value,
            field_name=mr.field_name,
            field_id=strawberry.ID(str(mr.field_id)) if mr.field_id else None,
            score=mr.score,
            skipped_reason=mr.skipped_reason,
        )
        for mr in match_results
    ]
    photo_note = (
        f"{images_attached} photo(s) attached"
        if images_attached
        else "no photos found in the PDF"
    )
    return build_mutation_response(
        types.ImportConnecteamRecapPdfResponse,
        success=True,
        message=(
            f"Drafted recap from PDF: {matched} field(s) imported, "
            f"{unmatched} unmatched, {photo_note}."
        ),
        input_obj=input_obj,
        custom_recap=recap,
        matched_count=matched,
        unmatched_count=unmatched,
        images_attached=images_attached,
        stats=stats,
    )


def _run_connecteam_import_sync(payload: dict) -> None:
    import asyncio

    from utils.db import fresh_db_connection

    def _run():
        asyncio.run(run_connecteam_import_task(payload))

    fresh_db_connection(_run)()


def enqueue_connecteam_import(payload: dict) -> None:
    enqueue_or_background(
        "/api/tasks/connecteam-import-recap",
        payload,
        lambda: _run_connecteam_import_sync(payload),
    )


async def run_connecteam_import_task(payload: dict) -> None:
    """Cloud Tasks / thread worker: download the PDF from GCS and import."""
    job_id = payload.get("job_id") or ""
    try:
        pdf_bytes = await sync_to_async(download_blob_bytes)(payload["blob_name"])
        if not pdf_bytes:
            raise ValueError("Uploaded PDF is empty or missing from storage.")
        user = await sync_to_async(User.objects.get)(id=payload["user_id"])
        event = await sync_to_async(
            Event.objects.select_related("tenant").get
        )(id=payload["event_id"])
        template = await sync_to_async(models.CustomRecapTemplate.objects.get)(
            id=payload["template_id"]
        )
        result = await execute_connecteam_import_from_bytes(
            user=user,
            event=event,
            template=template,
            pdf_bytes=pdf_bytes,
            name=payload.get("name"),
        )
        recap = getattr(result, "custom_recap", None)
        write_connecteam_job(
            job_id,
            {
                "status": "done" if getattr(result, "success", False) else "error",
                "message": getattr(result, "message", "") or "",
                "custom_recap_id": getattr(recap, "id", None),
                "custom_recap_uuid": str(getattr(recap, "uuid", "") or "") or None,
                "matched_count": getattr(result, "matched_count", 0) or 0,
                "unmatched_count": getattr(result, "unmatched_count", 0) or 0,
                "images_attached": getattr(result, "images_attached", 0) or 0,
            },
        )
    except Exception as exc:  # noqa: BLE001 — job status must always land
        logger.exception("connecteam-import task failed job=%s", job_id)
        try:
            write_connecteam_job(
                job_id,
                {"status": "error", "message": str(exc)},
            )
        except Exception:
            logger.exception("connecteam-import: could not write job error")

