"""Settings for the Spark MCP server, read from django.conf.settings with
safe defaults so the app works without any new env vars."""

from __future__ import annotations

from django.conf import settings

SCOPE = "spark.read"
SERVER_NAME = "Spark by Ignite"
SERVER_VERSION = "1.0.0"


def _get(name: str, default):
    return getattr(settings, name, default)


def enabled() -> bool:
    """Global kill switch (MCP_ENABLED, off unless set). Off → every MCP and
    OAuth endpoint answers 503 and existing tokens stop working until it is
    back on."""
    return bool(_get("MCP_ENABLED", False))


def base_url() -> str:
    """Public origin of the API (the OAuth issuer). No trailing slash."""
    raw = _get("MCP_PUBLIC_BASE_URL", "") or _get("PUBLIC_API_BASE_URL", "")
    return str(raw).rstrip("/")


def issuer() -> str:
    return base_url()


def resource_url() -> str:
    """Canonical MCP server URI (RFC 8707 resource / RFC 9728 resource)."""
    return f"{base_url()}/mcp"


def resource_metadata_url() -> str:
    return f"{base_url()}/.well-known/oauth-protected-resource/mcp"


def frontend_url() -> str:
    return str(
        _get("ADMIN_FRONTEND_URL", "") or _get("CLIENT_FRONTEND_URL", "")
    ).rstrip("/")


def signin_frontend_url() -> str:
    """Web app that runs "Continue with Google" for the consent page. Brand
    users sign in on the client host, so it comes first."""
    return str(
        _get("MCP_SIGNIN_FRONTEND_URL", "")
        or _get("CLIENT_FRONTEND_URL", "")
        or _get("ADMIN_FRONTEND_URL", "")
    ).rstrip("/")


def signin_handoff_ttl_seconds() -> int:
    return 10 * 60


def access_token_ttl_seconds() -> int:
    return int(_get("MCP_ACCESS_TOKEN_TTL_SECONDS", 3600))


def refresh_token_ttl_seconds() -> int:
    return int(_get("MCP_REFRESH_TOKEN_TTL_DAYS", 30)) * 86400


def auth_code_ttl_seconds() -> int:
    return 300


def daily_calls_per_tenant() -> int:
    return int(_get("MCP_DAILY_CALLS_PER_TENANT", 500))


def rpc_per_minute_per_connection() -> int:
    """Every JSON-RPC request (initialize, ping, tools/list, tools/call)."""
    return int(_get("MCP_RPC_PER_MINUTE_PER_CONNECTION", 120))


def calls_per_minute_per_connection() -> int:
    return int(_get("MCP_CALLS_PER_MINUTE_PER_CONNECTION", 30))


def allowed_tenant_ids() -> set[int] | None:
    """Rollout allowlist (MCP_ALLOWED_TENANT_IDS). Brand ids ("17,18") → only
    those brands; "all" → every brand; empty or unset → no brand (deny by
    default, so turning MCP_ENABLED on alone exposes nothing). Returns None
    for "all"."""
    raw = _get("MCP_ALLOWED_TENANT_IDS", []) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    parts = [str(p).strip() for p in raw if str(p).strip()]
    if any(p.lower() in ("all", "*") for p in parts):
        return None
    ids = set()
    for p in parts:
        try:
            ids.add(int(p))
        except ValueError:
            continue
    return ids


# Exact callback URLs an OAuth client may send people back to. Loopback
# (http://localhost / 127.0.0.1, any port) is always allowed for desktop
# clients such as Claude Code and the MCP Inspector. Anything else must be
# one of these URLs exactly (no other paths or subdomains): Ignite approves
# integrations in writing (ToS), so a new AI app is added here deliberately.
#   Claude:  https://claude.ai|claude.com/api/mcp/auth_callback
#   ChatGPT: its stable connector callback, used when the server returns
#            the RFC 9207 ``iss`` parameter (this one does). ChatGPT's
#            fallback is a per-app https://chatgpt.com/connector/oauth/<id>;
#            add that exact URL via MCP_ALLOWED_REDIRECT_URIS if it shows up.
DEFAULT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
]


def allowed_redirect_uris() -> list[str]:
    raw = _get("MCP_ALLOWED_REDIRECT_URIS", None)
    if raw is None:
        return list(DEFAULT_REDIRECT_URIS)
    if isinstance(raw, str):
        raw = raw.split(",")
    return [u.strip() for u in raw if u.strip()]


# Hosts a Client ID Metadata Document may be fetched from. Same reasoning as
# the redirect hosts, and it keeps the fetch away from internal addresses.
DEFAULT_CIMD_HOSTS = ["claude.ai", "claude.com", "chatgpt.com", "openai.com"]


def allowed_cimd_hosts() -> list[str]:
    raw = _get("MCP_ALLOWED_CIMD_HOSTS", None)
    if raw is None:
        return list(DEFAULT_CIMD_HOSTS)
    if isinstance(raw, str):
        raw = raw.split(",")
    return [h.strip().lower() for h in raw if h.strip()]


DEFAULT_ALLOWED_ORIGINS = [
    "https://claude.ai",
    "https://claude.com",
    "https://chatgpt.com",
    "https://chat.openai.com",
]


def allowed_origins() -> list[str]:
    raw = _get("MCP_ALLOWED_ORIGINS", None)
    if raw is None:
        return list(DEFAULT_ALLOWED_ORIGINS)
    if isinstance(raw, str):
        raw = raw.split(",")
    return [o.strip().rstrip("/").lower() for o in raw if o.strip()]
