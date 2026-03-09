import django.contrib.sites.requests
import strawberry
import strawberry_django
from utils.gcs import extract_blob_name_from_url, generate_download_url
from .models import Tenant, Role, User, TenantTheme
from strawberry.relay import Node


@strawberry_django.type(Role)
class RoleType(Node):
    uuid: strawberry.auto
    name: strawberry.auto


@strawberry_django.type(Tenant)
class TenantType(Node):
    uuid: strawberry.auto
    name: strawberry.auto
    slug: strawberry.auto
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


@strawberry_django.type(User)
class SparkUserType(Node):
    uuid: strawberry.auto
    username: strawberry.auto
    email: strawberry.auto
    first_name: strawberry.auto
    last_name: strawberry.auto
    image: strawberry.auto


@strawberry_django.type(TenantTheme)
class TenantThemeType(Node):
    name: strawberry.auto
    color_scheme: strawberry.auto
    css_variables: strawberry.auto
    tenant: strawberry.auto
