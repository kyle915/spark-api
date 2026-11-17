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


# Cached workload-identity credentials for IAM signBlob signing. Refreshing
# them on every signature was a metadata-server round trip per URL on top of
# the signBlob call itself; the token is good for ~1h, so refresh only when
# google-auth says it is no longer valid.
_SIGNING_CREDS_LOCK = __import__("threading").Lock()
_SIGNING_CREDS: dict = {"credentials": None, "email": None}


def _iam_signing_args_for_default_credentials():
    """Return (service_account_email, access_token) suitable for
    v4 signing via the GCS library's IAM-Credentials-API delegated
    signing path.

    Workload-identity credentials on Cloud Run carry only a token —
    no private key — so the standard ``blob.generate_signed_url()``
    raises "you need a private key to sign credentials". The earlier
    workaround built an ``iam.Signer`` and passed it as ``credentials``,
    but newer google-cloud-storage versions reject that because a
    Signer isn't Credentials — same error, different wording.

    The fix is the library's first-class IAM-signing path: pass
    ``service_account_email`` + ``access_token`` and *omit*
    ``credentials``. The library hits the IAM Credentials API's
    signBlob endpoint server-side. Requires the SA to hold
    roles/iam.serviceAccountTokenCreator on itself (which spark-
    api-new-sa does — set up in PR #547 / task #136).

    Returns (None, None) on local dev where GS_CREDENTIALS JSON
    provides a real key — the standard signing path works there.
    """
    if settings.GS_CREDENTIALS:
        return None, None

    from google.auth import default as auth_default
    from google.auth.transport import requests as g_requests

    with _SIGNING_CREDS_LOCK:
        credentials = _SIGNING_CREDS["credentials"]
        cached_email = _SIGNING_CREDS["email"]
        if credentials is None:
            credentials, _ = auth_default()
            _SIGNING_CREDS["credentials"] = credentials
            cached_email = None
        if not getattr(credentials, "valid", False):
            credentials.refresh(g_requests.Request())
        token = getattr(credentials, "token", None)
        if cached_email and token:
            return cached_email, token
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
    token = getattr(credentials, "token", None)
    if not token:
        return None, None
    with _SIGNING_CREDS_LOCK:
        _SIGNING_CREDS["email"] = sa_email
    return sa_email, token


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

    sa_email, access_token = _iam_signing_args_for_default_credentials()
    if sa_email is not None and access_token is not None:
        # Cloud Run path — let the GCS library hit IAM Credentials
        # API's signBlob endpoint server-side. No `credentials=` arg
        # so the library uses the IAM-signing branch.
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method="PUT",
            content_type=content_type,
            service_account_email=sa_email,
            access_token=access_token,
        )

    # Local dev / explicit service-account JSON path — the credentials
    # already include the private key, standard signing works.
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="PUT",
        content_type=content_type,
    )


def generate_download_url(
    blob_name: str,
    expiration_minutes: int = 60,
    *,
    response_type: str | None = None,
    response_disposition: str | None = None,
) -> str:
    """
    Generate a signed URL for downloading/viewing a file from GCS.

    Args:
        blob_name: The path/name of the file in the bucket (e.g., "products/image.jpg")
        expiration_minutes: How long the URL should be valid (default 60 minutes)
        response_type: Optional Content-Type override (signed response-content-type)
        response_disposition: Optional Content-Disposition override. Playback
            uses ``inline`` so the browser plays the file instead of downloading.

    Returns:
        A signed URL that can be used to GET a file from GCS
    """
    client = get_gcs_client()
    bucket = client.bucket(settings.GS_BUCKET_NAME)
    blob = bucket.blob(blob_name)

    overrides: dict[str, str] = {}
    if response_type:
        overrides["response_type"] = response_type
    if response_disposition:
        overrides["response_disposition"] = response_disposition

    sa_email, access_token = _iam_signing_args_for_default_credentials()
    if sa_email is not None and access_token is not None:
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method="GET",
            service_account_email=sa_email,
            access_token=access_token,
            **overrides,
        )

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="GET",
        **overrides,
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


def blob_exists(blob_name: str | None) -> bool:
    """
    Return True if a blob exists in the configured GCS bucket.

    Cheap HEAD-style existence check (`blob.exists()`) — used by the
    recap-file `displayUrl` resolver to decide whether a converted
    `.jpg` sibling is available for a `.heic` original before pointing
    the frontend at it. Returns False (never raises) on any GCS error
    so a transient bucket hiccup degrades to "serve the original",
    not a 500.
    """
    if not blob_name:
        return False
    try:
        client = get_gcs_client()
        bucket = client.bucket(settings.GS_BUCKET_NAME)
        return bucket.blob(blob_name).exists()
    except Exception:
        return False


def public_url(blob_name: str | None, *, durable: bool = False) -> str | None:
    """
    The URL to hand a reader for a blob.

    With ``PRIVATE_MEDIA_ENABLED`` off (the default) this is the plain
    ``https://storage.googleapis.com/<bucket>/<key>`` URL the public-read
    bucket serves — unchanged from before. With it on, private prefixes
    become an app URL (``/api/public/media/o/<token>/<name>``) that 302s to a
    short-lived signed URL; see ``utils.private_media``.

    ``durable=True`` is for links written somewhere that is read later
    (client spreadsheets/CSVs, emailed or printed links): the token does not
    expire. Anything rendered per request (GraphQL, REST, share pages) uses
    the default interactive token. Pure CPU either way — no GCS/IAM call.
    """
    from utils.private_media import media_url

    return media_url(blob_name, durable=durable)


def extract_blob_name_from_url(url_or_path: str | None) -> str | None:
    """
    Extract the blob path from a GCS signed URL or return the path as-is.
    """
    if not url_or_path:
        return url_or_path

    # App media URLs (/api/public/media/o/<token>/<name>) decode back to the
    # key they were minted for — signature checked, expiry ignored — so a
    # client echoing a URL it was given into a write (mobile
    # updateBaProfile.eventPhotos does) keeps storing the real key. A forged
    # or foreign-key token yields None, never the raw path.
    from utils.private_media import blob_from_media_url, is_media_url

    if is_media_url(url_or_path):
        return blob_from_media_url(url_or_path)

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
