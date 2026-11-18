from django.urls import path

from digest.models import record_cron_run

from .cron_views import AbuseUsageWatchView


def _heartbeat(view):
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        if getattr(response, "status_code", 0) not in (401, 403):
            record_cron_run("abuse-usage-watch", status=response.status_code)
        return response

    return wrapped


urlpatterns = [
    path("abuse-usage-watch", _heartbeat(AbuseUsageWatchView.as_view()), name="abuse-usage-watch"),
]
