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
        "api/v483927/graphql/spark",
        csrf_exempt(
            AsyncGraphQLView.as_view(
                schema=schema_spark,
                graphiql=settings.DEBUG,
            )
        ),
    ),
    path(
        "api/v615204/graphql/clients",
        csrf_exempt(
            AsyncGraphQLView.as_view(
                schema=schema_clients,
                graphiql=settings.DEBUG,
            )
        ),
    ),
    path(
        "api/v839471/graphql/ambassadors",
        csrf_exempt(
            AsyncGraphQLView.as_view(
                schema=schema_ambassador,
                graphiql=settings.DEBUG,
            )
        ),
    ),
    path(
        "api/v270986/graphql/mobile",
        csrf_exempt(
            AsyncGraphQLView.as_view(
                schema=schema_mobile,
                graphiql=settings.DEBUG,
            )
        ),
    ),
    path(
        "api/v348263/graphql/mobile",
        csrf_exempt(
            AsyncGraphQLView.as_view(
                schema=schema_mobile,
                graphiql=settings.DEBUG,
            )
        ),
    ),
    # Internal cron endpoints — `X-Cron-Secret` header guards each.
    # The path is intentionally not under /api/ so casual scanners
    # looking for GraphQL surface don't trip on it. See
    # `digest/cron_views.py` for the secret-validation flow.
    path("internal/cron/", include("digest.urls")),
    # Public, token-authenticated endpoints (no JWT). These power the
    # one-click "Review & approve" email flow — clients click a signed
    # link and land on a page they can actually act on, even if they're
    # not logged into Spark. See `events/views.py` for the contract.
    path("api/public/", include("events.urls")),
]

# Add RQ dashboard in DEBUG mode
if settings.DEBUG:
    urlpatterns += [
        path("django-rq/", include("django_rq.urls")),
    ]
