"""
URL config for the digest app's internal cron endpoints.

Mounted at `/internal/cron/` from `config/urls.py`. Each endpoint
guards itself with `X-Cron-Secret`; see `cron_views.py` for the
secret check.
"""

from django.urls import path

from .cron_views import _registered_views

urlpatterns = [
    path(f"{name}", view.as_view(), name=f"digest-cron-{name}")
    for name, view in _registered_views().items()
]
