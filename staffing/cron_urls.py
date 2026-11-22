from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from digest.urls import _heartbeat
from staffing.cron import NoShowCascadeCronView

urlpatterns = [
    path(
        "noshow-cascade",
        csrf_exempt(_heartbeat("noshow-cascade", NoShowCascadeCronView.as_view())),
        name="staffing-cron-noshow-cascade",
    ),
]
