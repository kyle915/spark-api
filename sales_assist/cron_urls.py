from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from digest.urls import _heartbeat
from sales_assist.cron import SalesAssistView

urlpatterns = [
    path(
        "sales-assist",
        csrf_exempt(_heartbeat("sales-assist", SalesAssistView.as_view())),
        name="sales-assist-cron",
    ),
]
