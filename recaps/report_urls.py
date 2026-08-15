"""Public (no-JWT) URL patterns for campaign reports and shared recaps.

Mounted under the ``/api/public/`` prefix in ``config/urls.py`` — the same
token-authenticated, cookie-free surface as the events approval flow and
the receipts upload flow.

Campaign report (``reports.campaign.v1`` token):

* GET /api/public/report/<token>        → report JSON
* GET /api/public/report/<token>/pdf    → branded report PDF

Shared recap (``recaps.share.v1`` token) — both paths must hit
``share_views``, not the campaign-report placeholders:

* GET /api/public/recap/<token>         → recap JSON
* GET /api/public/recap/<token>/pdf     → branded recap PDF
"""

from django.urls import path

from recaps import report_views, share_views

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
    path(
        "recap/<str:token>/pdf",
        share_views.public_recap_pdf_view,
        name="recaps.public_recap_pdf",
    ),
    path(
        "recap/<str:token>/signoff",
        share_views.public_recap_signoff_view,
        name="recaps.public_recap_signoff",
    ),
    path(
        "recap/<str:token>",
        share_views.public_recap_view,
        name="recaps.public_recap",
    ),
]
