"""Documents uploaded to Spark for a brand, as answerable text.

Where a brand's documents live in Spark (all read per tenant, nothing else):

* Training hub resources (``academy.TrainingResource`` on an active hub):
  PDFs, "page" items, and links that point straight at a .pdf/.docx/.txt.
  Videos and ordinary web links are skipped.
* Check-in resources (``Tenant.checkin_resources``, plus the legacy
  ``checkin_training_url``) that are PDFs / Word files.
* BA briefing templates (``jobs.BriefingTemplate`` title + body) and their
  attachments, plus attachments cloned onto the brand's gigs
  (``jobs.JobBriefingAttachment``), de-duplicated by URL.

Deliberately NOT read: ambassador compliance documents (``documents`` app —
IDs, certificates), recap photos/files, chat attachments, and per-gig
briefing bodies (they carry store addresses and contacts). Images are
skipped. A document is re-extracted only when its bytes change.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from django.conf import settings

from .extract import bytes_to_text, clean_text, clip, DOC_TEXT_MAX_CHARS
from .fetch import DOC_MAX_BYTES, Deadline, Fetcher
from .models import KnowledgeSource
from .store import existing, save_source, sha256, touch

logger = logging.getLogger(__name__)

MAX_DOCUMENTS = 40
_DOC_EXT = (".pdf", ".docx", ".txt", ".md")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".svg", ".avif")


@dataclass
class DocRef:
    kind: str
    ref: str
    title: str
    location: str = ""  # absolute URL or GCS blob path
    is_blob: bool = False
    content_type: str = ""
    size: int | None = None
    inline_text: str | None = None


def _path(url: str) -> str:
    return urlsplit(url or "").path.lower()


def _looks_like_doc(url: str) -> bool:
    return _path(url).endswith(_DOC_EXT)


def _is_image(url: str, content_type: str = "") -> bool:
    return (content_type or "").lower().startswith(
        ("image/", "video/", "audio/")
    ) or _path(url).endswith(_IMAGE_EXT)


def _absolute_frontend(url: str) -> str:
    """Training/check-in resource URLs may be root-relative to the BA site."""
    url = (url or "").strip()
    if url.startswith("/") and not url.startswith("//"):
        base = (
            getattr(settings, "PUBLIC_CHECKIN_BASE_URL", "")
            or "https://client.igniteproductions.co"
        )
        return urljoin(base.rstrip("/") + "/", url.lstrip("/"))
    return url


def _url_key(url: str) -> str:
    return hashlib.sha1((url or "").strip().encode("utf-8")).hexdigest()[:16]


def _attachment_location(url: str) -> tuple[str, bool]:
    """(location, is_blob) for a briefing attachment: GCS path or URL."""
    from utils.gcs import extract_blob_name_from_url

    url = (url or "").strip()
    low = url.lower()
    if low.startswith(("http://", "https://")):
        host = (urlsplit(url).hostname or "").lower()
        if host.endswith("storage.googleapis.com") or "/api/public/media/" in low:
            blob = extract_blob_name_from_url(url)
            if blob:
                return blob, True
        return url, False
    return url.lstrip("/"), True


def discover(tenant) -> list[DocRef]:
    from academy.models import TrainingResource
    from jobs.models import (
        BriefingTemplate,
        BriefingTemplateAttachment,
        JobBriefingAttachment,
    )
    from tenants.models import normalize_checkin_resources

    refs: list[DocRef] = []
    seen_urls: set[str] = set()

    def add(doc: DocRef) -> None:
        key = doc.location if doc.location else doc.ref
        if key in seen_urls:
            return
        seen_urls.add(key)
        refs.append(doc)

    resources = (
        TrainingResource.objects.filter(
            hub__tenant_id=tenant.id, hub__is_active=True, published=True
        )
        .exclude(kind="video")
        .order_by("hub_id", "order", "id")
    )
    for res in resources:
        url = _absolute_frontend(res.url)
        if not url or _is_image(url):
            continue
        if res.kind == "link" and not _looks_like_doc(url):
            continue
        add(
            DocRef(
                kind=KnowledgeSource.KIND_TRAINING_DOC,
                ref=f"training_resource:{res.id}",
                title=(res.title or "Training document").strip(),
                location=url,
            )
        )

    entries = normalize_checkin_resources(getattr(tenant, "checkin_resources", None))
    legacy = (getattr(tenant, "checkin_training_url", "") or "").strip()
    if legacy:
        entries.append({"label": "Training guide", "kind": "link", "url": legacy})
    for entry in entries:
        url = _absolute_frontend(entry["url"])
        if _is_image(url, "") or entry.get("kind") == "image":
            continue
        if entry.get("kind") != "pdf" and not _looks_like_doc(url):
            continue
        add(
            DocRef(
                kind=KnowledgeSource.KIND_CHECKIN_RESOURCE,
                ref=f"checkin_resource:{_url_key(url)}",
                title=entry["label"],
                location=url,
            )
        )

    for tpl in BriefingTemplate.objects.filter(
        tenant_id=tenant.id, is_archived=False
    ).order_by("id"):
        body = clean_text(f"{tpl.title}\n{tpl.body}")
        if body.strip():
            refs.append(
                DocRef(
                    kind=KnowledgeSource.KIND_BRIEFING,
                    ref=f"briefing_template:{tpl.id}",
                    title=(tpl.title or tpl.name or "BA briefing").strip(),
                    inline_text=clip(body, DOC_TEXT_MAX_CHARS),
                )
            )

    attachments = list(
        BriefingTemplateAttachment.objects.filter(
            template__tenant_id=tenant.id, template__is_archived=False
        ).order_by("id")
    ) + list(
        JobBriefingAttachment.objects.filter(job__tenant_id=tenant.id).order_by("-id")[
            :200
        ]
    )
    for att in attachments:
        if _is_image(att.url, att.content_type):
            continue
        location, is_blob = _attachment_location(att.url)
        if not location:
            continue
        add(
            DocRef(
                kind=KnowledgeSource.KIND_BRIEFING_ATTACHMENT,
                ref=f"briefing_attachment:{_url_key(location)}",
                title=(att.name or "Briefing attachment").strip(),
                location=location,
                is_blob=is_blob,
                content_type=att.content_type or "",
                size=att.size,
            )
        )
    return refs[:MAX_DOCUMENTS]


def _read_blob(path: str) -> bytes | None:
    from utils.gcs import download_blob_bytes

    return download_blob_bytes(path)


def refresh_documents(tenant, fetcher: Fetcher, *, deadline: Deadline) -> dict:
    stats = {
        "documents": 0,
        "changed": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": 0,
        "pruned": 0,
        "complete": False,
        "message": "",
    }
    docs = discover(tenant)
    wanted = {d.ref for d in docs}
    for doc in docs:
        if deadline.expired:
            stats["message"] = "Stopped at the time limit."
            break
        prior = existing(tenant.id, doc.ref)
        if prior is not None and prior.excluded:
            stats["skipped"] += 1
            continue
        stats["documents"] += 1
        if doc.inline_text is not None:
            _row, changed = save_source(
                tenant_id=tenant.id,
                kind=doc.kind,
                ref=doc.ref,
                title=doc.title,
                text=doc.inline_text,
            )
            stats["changed" if changed else "unchanged"] += 1
            continue
        if doc.size and doc.size > DOC_MAX_BYTES:
            save_source(
                tenant_id=tenant.id,
                kind=doc.kind,
                ref=doc.ref,
                title=doc.title,
                url="" if doc.is_blob else doc.location,
                error="too-large",
            )
            stats["errors"] += 1
            continue
        content_type = doc.content_type
        if doc.is_blob:
            try:
                data = _read_blob(doc.location)
            except Exception:  # noqa: BLE001
                logger.info(
                    "knowledge blob read failed tenant=%s", tenant.id, exc_info=True
                )
                data = None
            error = "" if data else "file-not-found"
            if data and len(data) > DOC_MAX_BYTES:
                data, error = None, "too-large"
        else:
            res = fetcher.get(doc.location, max_bytes=DOC_MAX_BYTES)
            data = res.data if res.ok and not res.truncated else None
            error = res.error or ("too-large" if res.truncated else "")
            content_type = res.content_type or content_type
        public_url = "" if doc.is_blob else doc.location
        if data is None:
            save_source(
                tenant_id=tenant.id,
                kind=doc.kind,
                ref=doc.ref,
                title=doc.title,
                url=public_url,
                error=error or "fetch-failed",
            )
            stats["errors"] += 1
            continue
        raw_hash = sha256(data)
        if prior is not None and prior.raw_hash == raw_hash and not prior.error:
            touch(prior)
            stats["unchanged"] += 1
            continue
        extracted = bytes_to_text(data, content_type, doc.location)
        if not extracted["kind"]:
            stats["skipped"] += 1
            continue
        _row, changed = save_source(
            tenant_id=tenant.id,
            kind=doc.kind,
            ref=doc.ref,
            title=doc.title,
            url=public_url,
            text=extracted["text"],
            raw_hash=raw_hash,
        )
        stats["changed" if changed else "unchanged"] += 1
    else:
        stats["complete"] = True
    if stats["complete"]:
        # Documents removed from Spark stop answering questions.
        doc_kinds = [
            k
            for k, _ in KnowledgeSource.KIND_CHOICES
            if k not in KnowledgeSource.WEBSITE_KINDS
        ]
        stale = KnowledgeSource.objects.filter(
            tenant_id=tenant.id, kind__in=doc_kinds
        ).exclude(ref__in=wanted)
        stats["pruned"] = stale.count()
        stale.delete()
    return stats
