from django.urls import path, re_path

from . import oauth, transport

urlpatterns = [
    path("mcp", transport.mcp_view, name="spark_mcp"),
    path("mcp/", transport.mcp_view),
    re_path(
        r"^\.well-known/oauth-protected-resource(?P<suffix>/mcp)?/?$",
        oauth.protected_resource_view,
        name="spark_mcp_prm",
    ),
    path(
        ".well-known/oauth-authorization-server",
        oauth.authorization_server_view,
        name="spark_mcp_asm",
    ),
    path("oauth/register", oauth.register_view, name="spark_mcp_register"),
    path("oauth/authorize", oauth.authorize_view, name="spark_mcp_authorize"),
    path("oauth/authorize/link", oauth.signin_link_view, name="spark_mcp_link"),
    path("oauth/token", oauth.token_view, name="spark_mcp_token"),
    path("oauth/revoke", oauth.revoke_view, name="spark_mcp_revoke"),
]
