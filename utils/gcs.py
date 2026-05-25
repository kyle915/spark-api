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


def _iam_signer_for_default_credentials():
    """Return (signer, service_account_email) usable for v4 signing
    when running under Cloud Run's workload-identity credentials.

    Workload-identity credentials carry only a token — no private key
    — so `blob.generate_signed_url()` raises "you need a private key
    to sign credentials". We work around that by routing signing
    through the IAM Credentials API's signBlob endpoint. Requires
    the Cloud Run service account to hold
    roles/iam.serviceAccountTokenCreator on itself (which spark-api-
    new-sa does).

    Returns (None, None) on local dev where GS_CREDENTIALS JSON
    provides a real key — the standard signing path works there.
    """
    if settings.GS_CREDENTIALS:
        return None, None

    from google.auth import default as auth_default
    from google.auth import iam
    from google.auth.transport import requests as g_requests

    credentials, _ = auth_default()
    auth_request = g_requests.Request()
    credentials.refresh(auth_request)
    sa_email = getattr(credentials, "service_account_email", None)
    if not sa_email or sa_email == "default":
        # Cloud Run's compute_engine Credentials sometimes report
        # "default" instead of the real email — pull it from the
        # metadata server in that case.
        try:
            import requests as _http

            resp = _http.get(
                "http://metadata.google.internal/computeMetadata/v1/"
                "instance/service-accounts/default/email",
                headers={"Metadata-Flavor": "Google"},
                timeout=2,
            )
            if resp.ok:
                sa_email = resp.text.strip()
        except Exception:
            return None, None
    if not sa_email:
        return None, None
    signer = iam.Signer(auth_request, credentials, sa_email)
    return signer, sa_email


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

    signer, sa_email = _iam_signer_for_default_credentials()
    if signer is not None and sa_email is not None:
        # Cloud Run path — IAM Credentials API signs on behalf of
        # the SA. No private key needed in the container.
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method="PUT",
            content_type=content_type,
            credentials=signer,
            service_account_email=sa_email,
        )

    # Local dev / explicit service-account JSON path — the credentials
    # already include the private key, standard signing works.
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="PUT",
        content_type=content_type,
    )


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

    signer, sa_email = _iam_signer_for_default_credentials()
    if signer is not None and sa_email is not None:
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method="GET",
            credentials=signer,
            service_account_email=sa_email,
        )

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="GET",
    )


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


def public_url(blob_name: str | None) -> str | None:
    """
    Return a non-signed, publicly-loadable URL for a blob.

    Use this for tenant logos, product images, and recap files where the
    bucket grants allUsers:objectViewer (uniform bucket access). Avoids
    the "you need a private key to sign credentials" failure that
    generate_signed_url() triggers on Cloud Run service accounts.
    """
    if not blob_name:
        return None
    bucket = getattr(__import__("django.conf", fromlist=["settings"]).settings, "GS_BUCKET_NAME", "")
    if not bucket:
        return None
    cleaned = blob_name.lstrip("/")
    return f"https://storage.googleapis.com/{bucket}/{cleaned}"


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
