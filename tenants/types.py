import django.contrib.sites.requests
import strawberry
import strawberry_django
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
