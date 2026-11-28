import functools

from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from digest.models import record_cron_run

from .cron_views import AbuseUsageWatchView


def _heartbeat(view):
    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        if getattr(response, "status_code", 0) not in (401, 403):
            record_cron_run("abuse-usage-watch", status=response.status_code)
        return response

    return wrapped


urlpatterns = [
    # csrf_exempt must wrap the OUTER callable: as_view() marks itself exempt, but
    # CsrfViewMiddleware only inspects the resolved callback (this closure), so
    # without it every cron POST got a 403 (same landmine as digest/urls.py).
    path(
        "abuse-usage-watch",
        csrf_exempt(_heartbeat(AbuseUsageWatchView.as_view())),
        name="abuse-usage-watch",
    ),
]
