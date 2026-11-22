from django.urls import path

from pilot.views import (
    public_provider_submission_view,
    public_workspace_shared_link_view,
)

urlpatterns = [
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
]
