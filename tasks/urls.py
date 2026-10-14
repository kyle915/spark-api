"""URL config for Cloud Tasks handler endpoints.

Mounted at `/api/tasks/` from `config/urls.py`. Each endpoint guards itself
with the `X-Tasks-Secret` shared secret; see `tasks/views.py` for the check.
"""

from django.urls import path

from tasks import views
from timeclock.views import clock_sheet_sync_task

urlpatterns = [
    path(
        "recap-approved-notify",
        views.recap_approved_notify_view,
        name="tasks.recap_approved_notify",
    ),
    path(
        "heic-convert",
        views.heic_convert_view,
        name="tasks.heic_convert",
    ),
    path(
        "clock-sheet-sync",
        clock_sheet_sync_task,
        name="tasks.clock_sheet_sync",
    ),
    path(
        "connecteam-import-recap",
        views.connecteam_import_recap_view,
        name="tasks.connecteam_import_recap",
    ),
]
