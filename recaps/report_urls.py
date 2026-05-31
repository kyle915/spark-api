"""Public (no-JWT) URL patterns for the Client Campaign Report.

Mounted under the ``/api/public/`` prefix in ``config/urls.py`` — the same
token-authenticated, cookie-free surface as the events approval flow and
the receipts upload flow. Two GET paths, both keyed off the
``reports.campaign.v1`` signed share token:

* GET /api/public/report/<token>        → report JSON
* GET /api/public/report/<token>/pdf    → branded report PDF

See ``recaps/report_views.py`` for the request/response contract.
"""

from django.urls import path

from recaps import report_views

urlpatterns = [
    # NOTE: the `/pdf` path is registered FIRST so it wins the match — a
    # bare `<str:token>` would otherwise greedily swallow "token/pdf".
    path(
        "report/<str:token>/pdf",
        report_views.public_report_pdf_view,
        name="recaps.public_report_pdf",
    ),
    path(
        "report/<str:token>",
        report_views.public_report_view,
        name="recaps.public_report",
    ),
]
