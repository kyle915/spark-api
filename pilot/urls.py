from django.urls import path

from pilot.views import (
    public_pos_feed_drop_view,
    public_provider_submission_view,
    public_sandbox_brand_view,
    public_workspace_shared_link_view,
    stripe_billing_webhook_view,
)

urlpatterns = [
    path(
        "sandbox-brand",
        public_sandbox_brand_view,
        name="public-sandbox-brand",
    ),
    path(
        "provider-submit/<str:code>",
        public_provider_submission_view,
        name="public-provider-submit",
    ),
    path(
        "workspace-share/<str:token>",
        public_workspace_shared_link_view,
        name="public-workspace-share",
    ),
    path(
        "stripe-billing/webhook",
        stripe_billing_webhook_view,
        name="stripe-billing-webhook",
    ),
    path(
        "pos-feed/<str:token>",
        public_pos_feed_drop_view,
        name="public-pos-feed-drop",
    ),
]
