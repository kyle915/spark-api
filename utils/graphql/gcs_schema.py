"""
GraphQL types and queries for Google Cloud Storage operations.
"""
import strawberry
from utils.gcs import generate_upload_url, public_url


@strawberry.type
class UploadUrlResponse:
    """Response containing a signed upload URL and the blob path."""
    upload_url: str
    blob_name: str


@strawberry.type
class GCSQuery:
    """Queries for Google Cloud Storage operations."""
    
    @strawberry.field
    def get_upload_url(
        self,
        file_path: str,
        content_type: str = "image/jpeg"
    ) -> UploadUrlResponse:
        """
        Get a signed URL for uploading a file to GCS.
        
        Args:
            file_path: The desired path in the bucket (e.g., "products/my-image.jpg")
            content_type: MIME type of the file (default: "image/jpeg")
        
        Returns:
            UploadUrlResponse with the signed URL and blob name
        """
        upload_url = generate_upload_url(file_path, content_type)
        return UploadUrlResponse(
            upload_url=upload_url,
            blob_name=file_path
        )
    
    @strawberry.field
    def get_download_url(self, file_path: str) -> str:
        """
        Get a signed URL for downloading/viewing a file from GCS.
        
        Args:
            file_path: The path of the file in the bucket (e.g., "products/my-image.jpg")
        
        Returns:
            A signed URL for accessing the file
        """
        # Public bucket — return unsigned URL, signing fails on Cloud Run.
        return public_url(file_path) or ""
