"""
GraphQL types and queries for Google Cloud Storage operations.
"""
import os
import re
import uuid

import strawberry
from graphql import GraphQLError

from utils.gcs import generate_upload_url, public_url
from utils.graphql.permissions import (
    StrictIsAuthenticated,
    _is_admin_access,
    resolve_request_user_access,
)


# Bucket prefixes a caller may upload under. The caller's ``filePath`` is
# only used to pick one of these (longest match on its leading segments) and
# to keep the file extension; the object name itself is always generated
# here, so a caller can never target (overwrite) an existing object.
# Keep in sync with the web + mobile upload helpers that call getUploadUrl.
UPLOAD_PREFIXES: tuple[str, ...] = (
    "recaps/receipts",
    "recaps",
    "chat_attachments",
    "receipt-campaign-images",
    "products",
    "connecteam-imports",
    "ba",
    "documents",
)
DEFAULT_UPLOAD_PREFIX = "uploads"

# Server-owned folders under an upload prefix that callers must not write to.
_RESERVED_SUBFOLDERS = {("connecteam-imports", "jobs")}

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def build_upload_blob_name(file_path: str | None) -> str:
    """Server-chosen blob name for an upload requested as ``file_path``.

    ``"{prefix}/{sub}/{uuid4}{ext}"`` where ``prefix`` is the whitelisted
    prefix the requested path starts with (``uploads`` when none matches),
    ``sub`` is the next path segment when it is a plain token (keeps the
    existing ``receipt-campaign-images/<kind>/`` and ``ba/<folder>/``
    layouts readable), and ``ext`` is the requested file's extension.
    """
    raw = (file_path or "").strip().replace("\\", "/")
    segments = [seg for seg in raw.split("/") if seg not in ("", ".", "..")]
    filename = segments[-1] if segments else ""
    dirs = segments[:-1]

    prefix = DEFAULT_UPLOAD_PREFIX
    consumed = 0
    for candidate in sorted(UPLOAD_PREFIXES, key=len, reverse=True):
        parts = candidate.split("/")
        if dirs[: len(parts)] == parts:
            prefix = candidate
            consumed = len(parts)
            break

    sub = ""
    if prefix != DEFAULT_UPLOAD_PREFIX and len(dirs) > consumed:
        candidate_sub = dirs[consumed]
        if _SEGMENT_RE.match(candidate_sub) and (
            prefix, candidate_sub
        ) not in _RESERVED_SUBFOLDERS:
            sub = candidate_sub

    ext = os.path.splitext(filename)[1].lower()
    if not _EXT_RE.match(ext or ""):
        ext = ""

    parts = [prefix] + ([sub] if sub else []) + [f"{uuid.uuid4().hex}{ext}"]
    return "/".join(parts)


# Prefixes a NON-admin may ask getDownloadUrl about: brand-facing catalog
# and branding assets only (product images, receipt-campaign artwork,
# tenant logos). Everything else in the bucket (BA documents and profile
# files under ba/ + documents/ + users/, chat attachments, receipts,
# Connecteam imports, recap files) is admin-only through this resolver.
# No web or mobile screen calls getDownloadUrl today; widen this only for a
# real caller, and prefer per-row access checks over a broader prefix.
NON_ADMIN_DOWNLOAD_PREFIXES: tuple[str, ...] = (
    "products/",
    "receipt-campaign-images/",
    "tenants/images/",
)


def normalize_download_path(file_path: str | None) -> str | None:
    """Bucket object key for ``file_path``, or None when it is not a plain
    relative key (empty, a URL, or containing ``.``/``..`` segments)."""
    raw = (file_path or "").strip()
    if not raw or "\\" in raw or "://" in raw or raw.startswith("//"):
        return None
    key = raw.lstrip("/")
    segments = key.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return None
    return key


def non_admin_may_download(key: str) -> bool:
    return key.startswith(NON_ADMIN_DOWNLOAD_PREFIXES)


@strawberry.type
class UploadUrlResponse:
    """Response containing a signed upload URL and the blob path."""
    upload_url: str
    blob_name: str


@strawberry.type
class GCSQuery:
    """Queries for Google Cloud Storage operations."""

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    def get_upload_url(
        self,
        file_path: str,
        content_type: str = "image/jpeg"
    ) -> UploadUrlResponse:
        """
        Get a signed URL for uploading a file to GCS.

        Args:
            file_path: The requested path (e.g., "products/my-image.jpg").
                Only its leading folder (matched against UPLOAD_PREFIXES)
                and its extension are used; the object name is generated
                server-side so an upload never replaces an existing file.
            content_type: MIME type of the file (default: "image/jpeg")

        Returns:
            UploadUrlResponse with the signed URL and the server-chosen
            blob name. Callers must store ``blobName``, not their own path.
        """
        blob_name = build_upload_blob_name(file_path)
        upload_url = generate_upload_url(blob_name, content_type)
        return UploadUrlResponse(
            upload_url=upload_url,
            blob_name=blob_name
        )

    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def get_download_url(self, info: strawberry.Info, file_path: str) -> str:
        """
        URL for viewing a file in the GCS bucket.

        Authenticated callers only. Spark admins (DB-backed
        ``_is_admin_access``) may ask for any object key; everyone else only
        for keys under ``NON_ADMIN_DOWNLOAD_PREFIXES`` (catalog/branding
        assets). The bucket is public-read, so this returns the plain public
        URL (signing fails on Cloud Run).

        Args:
            file_path: Object key in the bucket (e.g. "products/my-image.jpg").
        """
        key = normalize_download_path(file_path)
        if key is None:
            raise GraphQLError("Invalid file path.")
        user = info.context.request.user
        role_slug, is_staff, is_super, email = await resolve_request_user_access(
            user
        )
        if not _is_admin_access(role_slug, is_staff, is_super, email):
            if not non_admin_may_download(key):
                raise GraphQLError("You do not have access to this file.")
        return public_url(key) or ""
