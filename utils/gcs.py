"""
Google Cloud Storage utilities for generating signed URLs.
"""

from datetime import timedelta
from google.cloud import storage
from google.api_core.exceptions import NotFound
from django.conf import settings
from urllib.parse import urlparse
from typing import Optional


def get_gcs_client():
    """Get a GCS client instance."""
    if settings.GS_CREDENTIALS:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(
            settings.GS_CREDENTIALS
        )
        return storage.Client(project=settings.GS_PROJECT_ID, credentials=credentials)
    return storage.Client(project=settings.GS_PROJECT_ID)


def generate_upload_url(
    blob_name: str,
    content_type: str = "application/octet-stream",
    expiration_minutes: int = 15,
) -> str:
    """
    Generate a signed URL for uploading a file to GCS.

    Args:
        blob_name: The path/name of the file in the bucket (e.g., "products/image.jpg")
        content_type: The MIME type of the file being uploaded
        expiration_minutes: How long the URL should be valid (default 15 minutes)

    Returns:
        A signed URL that can be used to PUT a file to GCS
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GS_BUCKET_NAME)
    blob = bucket.blob(blob_name)

    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="PUT",
        content_type=content_type,
    )

    return url


def generate_download_url(blob_name: str, expiration_minutes: int = 60) -> str:
    """
    Generate a signed URL for downloading/viewing a file from GCS.

    Args:
        blob_name: The path/name of the file in the bucket (e.g., "products/image.jpg")
        expiration_minutes: How long the URL should be valid (default 60 minutes)

    Returns:
        A signed URL that can be used to GET a file from GCS
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GS_BUCKET_NAME)
    blob = bucket.blob(blob_name)

    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="GET",
    )

    return url


def delete_blob(blob_name: str) -> bool:
    """
    Delete a blob from the configured GCS bucket.

    Args:
        blob_name: The path/name of the file in the bucket (e.g., "products/image.jpg")

    Returns:
        True if the blob was deleted, False if it was not found.
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GS_BUCKET_NAME)
    blob = bucket.blob(blob_name)
    try:
        blob.delete()
        return True
    except NotFound:
        return False


def upload_bytes(
    blob_name: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> None:
    """
    Upload bytes directly to GCS.

    Args:
        blob_name: The path/name of the file in the bucket (e.g., "recaps/file.pdf")
        data: Byte content to upload
        content_type: MIME type for the uploaded blob
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GS_BUCKET_NAME)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)


def download_blob_bytes(blob_name: str) -> Optional[bytes]:
    """
    Download a blob from GCS as bytes.

    Args:
        blob_name: The path/name of the file in the bucket

    Returns:
        The blob content as bytes, or None if not found.
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GS_BUCKET_NAME)
    blob = bucket.blob(blob_name)
    try:
        return blob.download_as_bytes()
    except NotFound:
        return None


def extract_blob_name_from_url(url_or_path: str | None) -> str | None:
    """
    Extract the blob path from a GCS signed URL or return the path as-is.
    """
    if not url_or_path:
        return url_or_path

    parsed = urlparse(url_or_path)
    if not parsed.scheme:
        return url_or_path.lstrip("/")

    if parsed.scheme in ("http", "https"):
        path = parsed.path.lstrip("/")
        # Signed URLs typically look like https://storage.googleapis.com/<bucket>/<blob>?...
        if parsed.netloc.endswith("storage.googleapis.com"):
            parts = path.split("/", 1)
            if len(parts) == 2:
                return parts[1]
        return path

    if parsed.scheme == "gs":
        return parsed.path.lstrip("/")

    return url_or_path
