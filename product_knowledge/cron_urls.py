from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from digest.urls import _heartbeat
from product_knowledge.cron import RefreshProductKnowledgeView, SetBrandWebsitesView

urlpatterns = [
    path(
        "refresh-product-knowledge",
        csrf_exempt(
            _heartbeat(
                "refresh-product-knowledge", RefreshProductKnowledgeView.as_view()
            )
        ),
        name="product-knowledge-cron-refresh",
    ),
    path(
        "set-brand-websites",
        csrf_exempt(_heartbeat("set-brand-websites", SetBrandWebsitesView.as_view())),
        name="product-knowledge-cron-set-websites",
    ),
]
