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

urlpatterns = [
    # GET + POST. See events/views.py for the contract.
    path(
        "approval/<str:token>",
        views.public_approval_view,
        name="events.public_approval",
    ),
]
