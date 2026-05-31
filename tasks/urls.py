"""URL config for Cloud Tasks handler endpoints.

Mounted at `/api/tasks/` from `config/urls.py`. Each endpoint guards itself
with the `X-Tasks-Secret` shared secret; see `tasks/views.py` for the check.
"""

from django.urls import path

from tasks import views

urlpatterns = [
    path(
        "recap-approved-notify",
        views.recap_approved_notify_view,
        name="tasks.recap_approved_notify",
    ),
]
