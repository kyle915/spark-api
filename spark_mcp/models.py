"""Storage for the Spark MCP connection: OAuth clients, the user's grant of
one brand to one AI app ("connection"), authorization codes, hashed tokens,
and the per-call usage log that backs quotas and the audit trail.

Secrets are never stored: codes and tokens are random 256-bit values and only
their SHA-256 digest is kept, so a database read cannot be replayed against
the MCP endpoint.
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.db import models
from django.utils import timezone
from uuid6 import uuid7


def digest(secret: str) -> str:
    """SHA-256 hex digest of a code or token (what is stored and looked up)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class McpClient(models.Model):
    """An OAuth client: Claude, ChatGPT, or another MCP app.

    Created by dynamic client registration (RFC 7591) or on first use of a
    Client ID Metadata Document (``client_id`` is then the document's URL).
    Public clients only: no client secret is issued or stored.
    """

    KIND_DCR = "dcr"
    KIND_CIMD = "cimd"
    KIND_CHOICES = [(KIND_DCR, "Dynamic registration"), (KIND_CIMD, "Metadata document")]

    id = models.BigAutoField(primary_key=True)
    client_id = models.CharField(max_length=512, unique=True)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_DCR)
    client_name = models.CharField(max_length=200, blank=True, default="")
    client_uri = models.URLField(max_length=512, blank=True, default="")
    redirect_uris = models.JSONField(default=list)
    metadata = models.JSONField(default=dict, blank=True)
    disabled = models.BooleanField(
        default=False,
        help_text="Blocks new authorizations and every token issued to this client.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "AI app (OAuth client)"
        verbose_name_plural = "AI apps (OAuth clients)"

    def __str__(self) -> str:
        return self.client_name or self.client_id

    @property
    def display_name(self) -> str:
        return (self.client_name or "").strip() or "An AI app"


class McpConnection(models.Model):
    """One person's approval of one AI app for one brand.

    Every token hangs off a connection, so revoking the connection cuts off
    the app at once. A connection is pinned to exactly one tenant: the brand
    picked on the consent screen.
    """

    id = models.BigAutoField(primary_key=True)
    uuid = models.UUIDField(default=uuid7, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mcp_connections"
    )
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="mcp_connections"
    )
    client = models.ForeignKey(McpClient, on_delete=models.CASCADE, related_name="connections")
    scope = models.CharField(max_length=200, default="spark.read")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mcp_connections_revoked",
    )
    revoke_reason = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        verbose_name = "AI connection"
        verbose_name_plural = "AI connections"
        indexes = [models.Index(fields=["user", "revoked_at"])]

    def __str__(self) -> str:
        return f"{self.client} → {self.tenant} ({self.user})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def revoke(self, *, by=None, reason: str = "") -> None:
        """Revoke the connection and every token issued under it."""
        now = timezone.now()
        if self.revoked_at is None:
            self.revoked_at = now
            self.revoked_by = by
            self.revoke_reason = reason[:200]
            self.save(update_fields=["revoked_at", "revoked_by", "revoke_reason"])
        self.tokens.filter(revoked_at__isnull=True).update(revoked_at=now)


class McpAuthorizationCode(models.Model):
    id = models.BigAutoField(primary_key=True)
    code_hash = models.CharField(max_length=64, unique=True)
    connection = models.ForeignKey(McpConnection, on_delete=models.CASCADE, related_name="codes")
    redirect_uri = models.TextField()
    code_challenge = models.CharField(max_length=128)
    resource = models.CharField(max_length=512)
    scope = models.CharField(max_length=200)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class McpToken(models.Model):
    KIND_ACCESS = "access"
    KIND_REFRESH = "refresh"
    KIND_CHOICES = [(KIND_ACCESS, "Access"), (KIND_REFRESH, "Refresh")]

    id = models.BigAutoField(primary_key=True)
    token_hash = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    connection = models.ForeignKey(McpConnection, on_delete=models.CASCADE, related_name="tokens")
    resource = models.CharField(max_length=512)
    scope = models.CharField(max_length=200)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    # A refresh token is single use: rotation sets used_at. Presenting a used
    # one again is treated as theft and revokes the whole connection.
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    # The authorization code this token descends from (through refreshes),
    # so replaying that code revokes exactly these tokens.
    auth_code = models.ForeignKey(
        McpAuthorizationCode,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tokens",
    )

    class Meta:
        indexes = [models.Index(fields=["connection", "kind"])]


class McpToolCall(models.Model):
    """One MCP tool call: the audit trail and the quota counter."""

    STATUS_OK = "ok"
    STATUS_ERROR = "error"
    STATUS_QUOTA = "quota"
    STATUS_RATE = "rate_limited"
    STATUS_DENIED = "denied"
    STATUS_CHOICES = [
        (STATUS_OK, "OK"),
        (STATUS_ERROR, "Error"),
        (STATUS_QUOTA, "Daily quota reached"),
        (STATUS_RATE, "Rate limited"),
        (STATUS_DENIED, "Denied"),
    ]

    id = models.BigAutoField(primary_key=True)
    connection = models.ForeignKey(
        McpConnection, on_delete=models.SET_NULL, null=True, related_name="calls"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.SET_NULL, null=True, related_name="+"
    )
    client_name = models.CharField(max_length=200, blank=True, default="")
    tool = models.CharField(max_length=80)
    args_summary = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OK)
    result_count = models.IntegerField(null=True, blank=True)
    latency_ms = models.IntegerField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "AI tool call"
        verbose_name_plural = "AI tool calls"
        indexes = [
            models.Index(fields=["tenant", "created_at"]),
            models.Index(fields=["connection", "created_at"]),
        ]


class McpSignInHandoff(models.Model):
    """"Continue with Google" on the consent page, via the Spark web app.

    The consent page can't run Google sign-in itself (its host isn't an
    authorized origin on Spark's Google client), so it hands the person to
    the web app's normal sign-in and gets them back:

    1. The consent page stores the pending authorization request here with
       the digest of a random ``start`` nonce and the digest of a secret it
       keeps in this browser's session (``binding_hash``), then sends the
       browser to the web app with the start nonce.
    2. The web app signs the person in (Google), and an authenticated
       mutation marks the handoff approved for that user. It burns the start
       nonce and mints a fresh ``return`` nonce, which only that browser sees.
    3. The browser comes back to the consent page with the return nonce. It
       only finishes in the browser that started (session binding), once,
       within ``expires_at``.

    Only digests are stored, never the nonces themselves.
    """

    id = models.BigAutoField(primary_key=True)
    start_hash = models.CharField(max_length=64, unique=True)
    return_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    binding_hash = models.CharField(max_length=64)
    request = models.JSONField(default=dict)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    approved_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    refused_reason = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        verbose_name = "AI sign-in handoff"
        verbose_name_plural = "AI sign-in handoffs"
        indexes = [models.Index(fields=["expires_at"])]


class McpThrottle(models.Model):
    """Fixed-window counters for sign-in, registration and token throttles.

    In the database so every Cloud Run instance shares them (the old
    per-instance cache let each instance grant its own allowance). ``key`` is
    a SHA-256 digest of the bucket name, so emails and IPs aren't stored.
    """

    id = models.BigAutoField(primary_key=True)
    key = models.CharField(max_length=64, unique=True)
    window_start = models.DateTimeField()
    count = models.IntegerField(default=0)

    class Meta:
        verbose_name = "AI throttle counter"
        verbose_name_plural = "AI throttle counters"
        indexes = [models.Index(fields=["window_start"])]
