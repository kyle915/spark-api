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
    # Public, token-authenticated consumer receipt upload (no JWT). Shoppers
    # scan a per-event QR / open a link and upload a purchase receipt; the
    # token resolves to the event + tenant. See `receipts/views.py`.
    path("api/public/", include("receipts.urls")),
    # Public, token-authenticated Client Campaign Report (no JWT). A signed
    # share token resolves to a request's aggregate report (JSON) and the
    # branded report PDF. See `recaps/report_views.py`.
    path("api/public/", include("recaps.report_urls")),
    # Public, token-authenticated client invoice (no JWT). A signed share
    # token resolves to one invoice (camelCase JSON) and the branded invoice
    # PDF. See `billing/views.py`.
    path("api/public/", include("billing.urls")),
    # Cloud Tasks handler endpoints (no JWT, `X-Tasks-Secret` shared-secret
    # gated). The feature-flagged async path for recap approval enqueues a
    # task that POSTs here to run the client/RMM email + PDF in the
    # background. Fails closed (403) when the secret is unset. See
    # `tasks/views.py`.
    path("api/tasks/", include("tasks.urls")),
]

# Add RQ dashboard in DEBUG mode
if settings.DEBUG:
    urlpatterns += [
        path("django-rq/", include("django_rq.urls")),
    ]
