"""Public (no-JWT) URL patterns for a shared invoice.

Mounted under the ``/api/public/`` prefix in ``config/urls.py`` — the same
token-authenticated, cookie-free surface as the events approval flow, the
receipts upload flow, and the campaign-report flow. Two GET paths, both keyed
off the ``billing.invoice.v1`` signed share token:

* GET /api/public/invoice/<token>        → invoice JSON
* GET /api/public/invoice/<token>/pdf    → branded invoice PDF

See ``billing/views.py`` for the request/response contract.
"""

from django.urls import path

from billing import views

urlpatterns = [
    # NOTE: the `/pdf` path is registered FIRST so it wins the match — a bare
    # `<str:token>` would otherwise greedily swallow "token/pdf".
    path(
        "invoice/<str:token>/pdf",
        views.public_invoice_pdf_view,
        name="billing.public_invoice_pdf",
    ),
    path(
        "invoice/<str:token>",
        views.public_invoice_view,
        name="billing.public_invoice",
    ),
]
