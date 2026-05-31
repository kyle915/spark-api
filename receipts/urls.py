"""Public (no-JWT) URL patterns for consumer receipt upload.

Mounted under the `/api/public/` prefix in `config/urls.py` — the same
token-authenticated, cookie-free surface as the events public approval
flow. Two paths, both keyed off a per-event signed token:

* GET  /api/public/receipts/<token>            → event/brand display info
* POST /api/public/receipts/<token>/submit     → store image + create receipt

See `receipts/views.py` for the request/response contract.
"""

from django.urls import path

from receipts import views

urlpatterns = [
    # --- Campaign (GoToAisle-style) public surface, keyed by slug ---
    # GET — resolve a campaign slug to its public display info.
    path(
        "campaigns/<slug:slug>",
        views.public_campaign_view,
        name="receipts.public_campaign",
    ),
    # POST — submit a receipt image + optional consumer/Venmo fields.
    path(
        "campaigns/<slug:slug>/submit",
        views.public_campaign_submit_view,
        name="receipts.public_campaign_submit",
    ),
    # --- Legacy per-event surface, keyed by signed token (kept resolving) ---
    # GET — resolve token to event display info for the upload page.
    path(
        "receipts/<str:token>",
        views.public_receipt_event_view,
        name="receipts.public_event",
    ),
    # POST — submit the receipt image (multipart or base64) + optional fields.
    path(
        "receipts/<str:token>/submit",
        views.public_receipt_submit_view,
        name="receipts.public_submit",
    ),
]
