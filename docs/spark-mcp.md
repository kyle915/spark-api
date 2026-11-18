# Spark MCP server (AI app connections)

The official way for a brand to connect Claude, ChatGPT or another MCP app to
its Spark data. It replaces copying session tokens into scripts: the Terms of
Service allow automated access only through this connection (or another
integration Ignite approves in writing).

Code: `spark_mcp/` (OAuth 2.1 authorization server + Streamable HTTP MCP
endpoint). Brand-facing help page: `/help/ai-connector` in spark-front-client.

## For brands

**Connector URL:** `https://<api host>/mcp` — see "Launch" below for the host.

### Claude (claude.ai, Desktop, mobile)

1. Settings → Connectors → **Add custom connector**.
2. Name it "Spark" and paste the connector URL. Leave the OAuth client fields
   empty.
3. Click **Connect**. A Spark page opens: **Continue with Google** (or sign in
   with your Spark email and password, or ask for an email sign-in link), pick
   the brand, click **Allow**.
4. In a chat, turn the Spark connector on from the tools menu and ask, e.g.
   "How many consumers did we sample last month, by state?"

On Team and Enterprise plans an owner may need to add the connector for the
organization first.

### ChatGPT

1. Settings → Apps & Connectors → Advanced settings → turn on **Developer
   mode** (Business / Enterprise workspaces: an admin must allow custom
   connectors).
2. **Create** a connector: name "Spark", MCP server URL = the connector URL,
   authentication **OAuth**.
3. Sign in on the Spark page, pick the brand, **Allow**.

### What it can do (read only, one brand per connection)

| Tool | What it returns |
| --- | --- |
| `get_program_kpis` | Events with recaps, approved recaps, consumers sampled, samples, products sold, first-time / brand-aware / willing-to-purchase, plus the previous period of equal length |
| `list_recaps` | Approved recaps: date, store, state, activation type, consumers sampled, engagements, link to Spark (50 per call, paged) |
| `get_consumer_breakdown` | Consumers sampled by store, state or week; units sold and sampled by product |
| `list_upcoming_events` | Next 1-30 days of events and not-yet-scheduled requests, with a count of booked brand ambassadors |
| `list_pending_approvals` | Requests and recaps waiting on the brand (links only; approving happens in Spark) |
| `get_weekly_summary` | The latest published weekly summary (or a given week) |
| `get_connection_info` | Which brand this is and how many calls are left today |

It cannot change anything, see other brands, or see brand ambassadors'
personal information (names, phone, email, address, payment, documents,
location). There is no ambassador lookup.

### Limits

* 500 tool calls per brand per day (all people and apps on that brand
  together), resetting at midnight Pacific.
* 30 calls per minute per connection.
* Date ranges up to 366 days; recaps 50 per call; upcoming events up to 30
  days ahead and 100 rows.
* Access tokens last 1 hour and refresh automatically; a connection stays
  signed in for up to 30 days of inactivity.

### Disconnecting

Spark → Settings → **Connected AI apps** → Disconnect. Removing the
connector in Claude / ChatGPT stops that app using it; disconnecting in Spark
is what guarantees its access is cut off.

## Internal notes

### Launch checklist (Kyle)

1. Pick the public API host for the connector **before** brands connect: the
   OAuth issuer is `MCP_PUBLIC_BASE_URL` (falls back to `PUBLIC_API_BASE_URL`,
   the Cloud Run URL). Changing it later invalidates every connection (brands
   reconnect). Suggested: map a custom domain such as
   `mcp.sparkbyignite.igniteproductions.co` (or `api.` …) to the Cloud Run
   service, add it to `ALLOWED_HOSTS`, and set
   `MCP_PUBLIC_BASE_URL=https://<that host>`.
2. Deploy (migration `spark_mcp.0001_initial`, new app only).
3. Check discovery: `curl https://<host>/.well-known/oauth-protected-resource/mcp`
   and `curl -i -X POST https://<host>/mcp` (expect 401 with
   `WWW-Authenticate: Bearer resource_metadata=...`).
4. Connect it yourself in Claude and ChatGPT with a brand test account.
5. Optional staged rollout: `MCP_ALLOWED_TENANT_IDS=17,18`.
6. Publish the brand help page (front-client PR) and the updated Terms.
7. Directory listings are optional: brands can add it as a custom connector
   today. A listing in Claude's connector directory or ChatGPT's app directory
   needs a separate submission and review.

### Settings (env)

| Var | Default | Meaning |
| --- | --- | --- |
| `MCP_ENABLED` | `False` | Kill switch, off unless set. False → `/mcp` and `/oauth/*` return 503, tokens stop working |
| `MCP_PUBLIC_BASE_URL` | `PUBLIC_API_BASE_URL` | Issuer / connector origin |
| `MCP_ALLOWED_TENANT_IDS` | empty (no brand) | Rollout allowlist: brand ids (`17,18`), or `all`. Empty denies every brand |
| `MCP_DAILY_CALLS_PER_TENANT` | 500 | Per-brand daily quota |
| `MCP_CALLS_PER_MINUTE_PER_CONNECTION` | 30 | Per-connection tool-call rate limit |
| `MCP_RPC_PER_MINUTE_PER_CONNECTION` | 120 | Per-connection limit on every JSON-RPC request (initialize, ping, tools/list too) |
| `MCP_ACCESS_TOKEN_TTL_SECONDS` | 3600 | Access token life |
| `MCP_REFRESH_TOKEN_TTL_DAYS` | 30 | Refresh token life (rotated on use) |
| `MCP_ALLOWED_REDIRECT_URIS` | `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`, `https://chatgpt.com/connector_platform_oauth_redirect` | Exact callback URLs an app may send people back to (loopback always allowed for desktop clients). Replaces the old host list |
| `MCP_TRUSTED_PROXY_HOPS` | 1 | `X-Forwarded-For` entries added by trusted proxies; the client IP is read that many from the right |
| `MCP_ALLOWED_CIMD_HOSTS` | claude.ai, claude.com, chatgpt.com, openai.com | Hosts (exact, no subdomains) a Client ID Metadata Document may be fetched from |
| `MCP_ALLOWED_ORIGINS` | claude.ai, claude.com, chatgpt.com, chat.openai.com | Browser `Origin` values accepted on `/mcp` |
| `MCP_SIGNIN_FRONTEND_URL` | `CLIENT_FRONTEND_URL` | Web app that runs Continue with Google for the consent page |

Adding another AI app = adding its exact callback URL (and CIMD host if it
uses one). That is the "approved in writing" gate from the Terms.

### Who can connect

* Brand users (role `client`) and Ignite staff: only brands they are an
  active member of (`TenantedUser`). Staff have no "any brand" access here;
  to test against a brand, be a member of it.
* The account must be verified (gqlauth `UserStatus.verified`, the same gate
  as the app's password sign-in). Fix a stuck account with `verify_user`.
* Brand ambassadors and everyone else: nobody — the consent page says so.

### Continue with Google

The consent page can't run Google sign-in itself (its host isn't an
authorized JavaScript origin on Spark's Google client), so it hands off to the
web app and gets the person back (`McpSignInHandoff`):

1. **Continue with Google** stores the pending authorization request with the
   digest of a random start nonce and of a secret kept in this browser's
   session, then sends the browser to `<client app>/connect-ai?h=<start nonce>`.
2. The web app signs the person in with its normal Google button and calls
   `approveAiSignIn(handoff)` (authenticated). That burns the start nonce,
   checks the account (verified, member of a brand), and returns a URL on the
   API with a fresh return nonce, which only that browser sees.
3. `/oauth/authorize/google?h=<return nonce>` finishes the sign-in only in the
   browser that started it, once, within 10 minutes, then shows the brand
   picker. The return address is built on the server from
   `MCP_PUBLIC_BASE_URL`; no URL from the request is ever followed.

The web app host is `MCP_SIGNIN_FRONTEND_URL`, else `CLIENT_FRONTEND_URL`.

Membership is re-checked on every call: removing someone from a brand,
deactivating them, or dropping the brand from `MCP_ALLOWED_TENANT_IDS` cuts
the app off at once.

### Revoking

* A brand user: Settings → Connected AI apps → Disconnect.
* Ignite admins see **everyone's** connections in that same section and can
  disconnect any of them.
* Command line (Cloud Run job / local with prod DB):
  * `manage.py mcp_connections` — list active connections
  * `manage.py mcp_connections --revoke <uuid>`
  * `manage.py mcp_connections --revoke-user someone@brand.com`
  * `manage.py mcp_connections --revoke-tenant 17`
  * `manage.py mcp_connections --disable-client <client_id>` — block one AI
    app registration everywhere
  * `manage.py mcp_connections --usage 17` — last 7 days of calls by tool
* Everything at once: `MCP_ENABLED=False`.

(`spark_mcp/admin.py` registers the models, but the Django admin is not
mounted on this service.)

### Logging

Every tool call is a `spark_mcp.McpToolCall` row: user, tenant, AI app name,
tool, trimmed arguments, status (`ok`, `error`, `quota`, `rate_limited`,
`denied`), result count and latency. The quotas count these rows, so they hold
across Cloud Run instances.

TODO: when the shared abuse guardrails (rate limiter + audit log) land,
switch `spark_mcp/quota.py` to them; it is a two-function interface on
purpose.

### Protocol notes

* OAuth 2.1: authorization code + PKCE (S256 only), public clients, dynamic
  client registration (RFC 7591) and Client ID Metadata Documents, RFC 8414 /
  RFC 9728 metadata, RFC 8707 `resource` binding, RFC 9207 `iss` in the
  redirect, RFC 7009 revocation. Codes and tokens are stored as SHA-256
  hashes. Refresh tokens are single use; replaying one revokes the connection.
* MCP Streamable HTTP, stateless JSON responses. Serves both
  initialize-based clients (2025-03-26, 2025-06-18, 2025-11-25) and
  2026-07-28 clients (`server/discover`, per-request `_meta`, header
  validation, cache hints). Verified with the official MCP Python SDK 2.3 in
  legacy, 2026-07-28 and auto modes, including its own OAuth discovery,
  registration and PKCE.
* Brand ambassador privacy is enforced twice: no tool reads ambassador
  personal columns, and `spark_mcp/privacy.py` scrubs every returned string
  (emails, phone numbers, and the names of every ambassador who has worked
  for the brand). `spark_mcp/tests/test_mcp.py` scans every tool's output for
  ambassador fixtures.

### Abuse limits

Sign-in, registration and token throttles are counted in the database
(`McpThrottle`), so they hold across Cloud Run instances, and are keyed on the
client IP Cloud Run saw (right-most `X-Forwarded-For` hop), not the
caller-supplied left-most one. Password sign-in is limited per account (10
per 15 minutes, whatever the IP) and per IP. A consent-page sign-in lasts 10
minutes and one approval. Tool calls reserve their quota slot under a
per-brand database lock, so simultaneous calls can't overrun the daily quota.
