from django.urls import path, include
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from strawberry.django.views import AsyncGraphQLView

from .schema_ambassador import schema_ambassador
from .schema_client import schema_clients
from .schema_spark import schema_spark
from .schema_mobile import schema_mobile

urlpatterns = [
    path(
        "api/v1/graphql/spark",
        csrf_exempt(AsyncGraphQLView.as_view(schema=schema_spark)),
    ),
    path(
        "api/v1/graphql/clients",
        csrf_exempt(AsyncGraphQLView.as_view(schema=schema_clients)),
    ),
    path(
        "api/v1/graphql/ambassadors",
        csrf_exempt(AsyncGraphQLView.as_view(schema=schema_ambassador)),
    ),
    path(
        "api/v1/graphql/mobile",
        csrf_exempt(AsyncGraphQLView.as_view(schema=schema_mobile)),
    ),
]

# Add RQ dashboard in DEBUG mode
if settings.DEBUG:
    urlpatterns += [
        path('django-rq/', include('django_rq.urls')),
    ]
