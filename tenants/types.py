import django.contrib.sites.requests
import strawberry
import strawberry_django
from utils.gcs import extract_blob_name_from_url, generate_download_url
from .models import Tenant, Role

@strawberry_django.type(Role)
class RoleType:
    id: strawberry.auto
    uuid: strawberry.auto
    name: strawberry.auto

@strawberry_django.type(Tenant)
class TenantType:
    id: strawberry.auto
    uuid: strawberry.auto
    name: strawberry.auto
    request_url_name: strawberry.auto

    @strawberry.field
    def image(self) -> str | None:
        """Return a signed URL for the tenant image if it exists."""
        if not self.image:
            return None

        blob_name = extract_blob_name_from_url(self.image.name)
        if not blob_name:
            return None

        return generate_download_url(blob_name)
