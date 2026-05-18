import django.contrib.sites.requests
import strawberry
import strawberry_django
from asgiref.sync import sync_to_async
from utils.gcs import extract_blob_name_from_url, public_url
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

    @strawberry.field(name="image")
    def image_url(self) -> str | None:
        """Return the public URL for the tenant image if one exists.

        Aliased to GraphQL field `image` via name= so we can keep the
        Python method off the shadow path. Avoids an extra ORM round
        trip per row.
        """
        field_file = self.__dict__.get("image") or getattr(self, "image", None)
        if not field_file:
            return None
        try:
            blob = field_file.name
        except Exception:
            blob = str(field_file)
        blob_name = extract_blob_name_from_url(blob)
        return public_url(blob_name)


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
