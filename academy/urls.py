"""Public (code-gated) academy URLs.

Mounted under the ``/api/public/`` prefix in ``config/urls.py`` — the same
group as request approval, receipts, recap reports and web check-in. No JWT;
the code in the path is the only credential.

* GET /api/public/training/<code>  → the shareable BA training hub
"""
from django.urls import path

from academy import training_views

urlpatterns = [
    path(
        "training/<str:code>",
        training_views.public_training_hub,
        name="academy.public_training_hub",
    ),
]
