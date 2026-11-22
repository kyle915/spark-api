"""Public URL patterns for Buyer Pitch Decks (mounted under ``/api/public/``)."""

from django.urls import path

from brand_insights.pitch_views import public_pitch_deck_view

urlpatterns = [
    path(
        "pitch/<str:token>/",
        public_pitch_deck_view,
        name="brand_insights.public_pitch_deck",
    ),
    path(
        "pitch/<str:token>",
        public_pitch_deck_view,
        name="brand_insights.public_pitch_deck_noslash",
    ),
]
