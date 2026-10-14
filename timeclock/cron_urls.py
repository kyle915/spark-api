from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from digest.urls import _heartbeat
from timeclock.cron import ClockLinksCronView

urlpatterns = [
    path(
        "clock-links",
        csrf_exempt(_heartbeat("clock-links", ClockLinksCronView.as_view())),
        name="timeclock-cron-clock-links",
    ),
]
