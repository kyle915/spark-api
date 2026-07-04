"""
URL config for the digest app's internal cron endpoints.

Mounted at `/internal/cron/` from `config/urls.py`. Each endpoint
guards itself with `X-Cron-Secret`; see `cron_views.py` for the
secret check. Every hit is wrapped to record a CronRun heartbeat
(skipping auth denials) so System Health can show what actually fired.
"""

from django.urls import path

from .cron_views import _registered_views
from .models import record_cron_run


def _heartbeat(name, view_callable):
    def wrapped(request, *args, **kwargs):
        response = view_callable(request, *args, **kwargs)
        # Skip auth denials (401/403) — those are "someone hit it without the
        # secret", not a real run of the job.
        status = getattr(response, "status_code", 0)
        if status not in (401, 403):
            record_cron_run(name, status=status)
        return response

    return wrapped


urlpatterns = [
    path(
        f"{name}",
        _heartbeat(name, view.as_view()),
        name=f"digest-cron-{name}",
    )
    for name, view in _registered_views().items()
]
