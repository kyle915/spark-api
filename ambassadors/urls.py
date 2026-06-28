"""Public (token-gated) ambassador URLs.

Mounted under the ``/api/public/`` prefix in ``config/urls.py`` — the same
group as request approval, receipts, and recap reports. No JWT; the signed
token in the path is the only credential.

* GET  /api/public/extension/<token>   → one-click approval page
* POST /api/public/extension/<token>   → approve / decline the extension
"""
from django.urls import path

from ambassadors.views import ExtensionApprovalView

urlpatterns = [
    path(
        "extension/<str:token>",
        ExtensionApprovalView.as_view(),
        name="ambassadors.public_extension_approval",
    ),
]
