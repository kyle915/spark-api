from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from strawberry.django.views import AsyncGraphQLView

from .schema_ambassador import schema_ambassador
from .schema_client import schema_clients
from .schema_spark  import schema_spark

urlpatterns = [
    path('api/v1/graphql/spark', csrf_exempt(AsyncGraphQLView.as_view(schema=schema_spark))),
    path('api/v1/graphql/clients', csrf_exempt(AsyncGraphQLView.as_view(schema=schema_clients))),
    path('api/v1/graphql/ambassadors', csrf_exempt(AsyncGraphQLView.as_view(schema=schema_ambassador))),
]