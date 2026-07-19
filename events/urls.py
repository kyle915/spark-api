"""URL patterns for the events app.

Right now this file only carries the public approval endpoint —
everything else in events is exposed via GraphQL (`schema_spark` /
`schema_clients`). The /api/public/ prefix (set in `config/urls.py`) is
deliberately distinct from the JWT-protected /api/v<digits>/ endpoints so
ops can grep "public" in nginx / Cloud Run access logs and tell
token-authenticated traffic apart from cookie-authenticated traffic at a
glance.
"""

from django.urls import path

from events import views
from events import client_live_views
from events import checkin_views

urlpatterns = [
    # GET + POST. See events/views.py for the contract.
    path(
        "approval/<str:token>",
        views.public_approval_view,
        name="events.public_approval",
    ),
    # GET — public branded client-live page JSON (signed token = auth).
    path(
        "client-live/<str:token>",
        client_live_views.public_client_live_view,
        name="events.public_client_live",
    ),
    # Public web check-in (no login) — the event's walk-up code IS the link.
    # See events/checkin_views.py; the session token minted by /identify
    # authorizes the follow-up clock / upload-url / recap calls.
    path(
        "checkin/<str:code>",
        checkin_views.public_checkin_context,
        name="events.public_checkin_context",
    ),
    path(
        "checkin/<str:code>/identify",
        checkin_views.public_checkin_identify,
        name="events.public_checkin_identify",
    ),
    path(
        "checkin/<str:code>/clock",
        checkin_views.public_checkin_clock,
        name="events.public_checkin_clock",
    ),
    path(
        "checkin/<str:code>/upload-url",
        checkin_views.public_checkin_upload_url,
        name="events.public_checkin_upload_url",
    ),
    path(
        "checkin/<str:code>/recap",
        checkin_views.public_checkin_recap,
        name="events.public_checkin_recap",
    ),
]
