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

    @strawberry.field
    async def image(self) -> str | None:
        """Return the public URL for the tenant image if one exists.

        Reads the image FieldFile via a fresh Tenant.objects.get() to
        sidestep the name-shadow issue (this resolver is named `image`
        and would otherwise recurse into itself on `self.image`). Also
        skips signed-URL generation because the bucket is public — the
        signing path raises on Cloud Run service-account credentials.
        """
        row = await sync_to_async(
            Tenant.objects.only("id", "image").get, thread_sensitive=True
        )(pk=self.pk)
        field_file = row.image
        if not field_file:
            return None
        blob_name = extract_blob_name_from_url(field_file.name)
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
