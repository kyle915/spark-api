from django.contrib import admin, messages

from .models import McpClient, McpConnection, McpToolCall


@admin.register(McpClient)
class McpClientAdmin(admin.ModelAdmin):
    list_display = ("client_name", "kind", "client_id", "disabled", "created_at", "last_used_at")
    list_filter = ("kind", "disabled")
    search_fields = ("client_name", "client_id")
    readonly_fields = ("client_id", "kind", "redirect_uris", "metadata", "created_at", "updated_at", "last_used_at")
    fields = ("client_name", "client_uri", "disabled") + readonly_fields


@admin.action(description="Revoke selected connections (tokens stop working at once)")
def revoke_connections(modeladmin, request, queryset):
    n = 0
    for conn in queryset.filter(revoked_at__isnull=True):
        conn.revoke(by=request.user, reason="revoked by staff in admin")
        n += 1
    messages.success(request, f"Revoked {n} connection(s).")


@admin.register(McpConnection)
class McpConnectionAdmin(admin.ModelAdmin):
    list_display = ("uuid", "user", "tenant", "client", "created_at", "last_used_at", "revoked_at")
    list_filter = ("tenant", ("revoked_at", admin.EmptyFieldListFilter))
    search_fields = ("user__email", "tenant__name", "client__client_name")
    raw_id_fields = ("user", "tenant", "client", "revoked_by")
    readonly_fields = ("uuid", "user", "tenant", "client", "scope", "created_at", "last_used_at",
                       "revoked_at", "revoked_by", "revoke_reason")
    actions = [revoke_connections]

    def has_add_permission(self, request):
        return False


@admin.register(McpToolCall)
class McpToolCallAdmin(admin.ModelAdmin):
    list_display = ("created_at", "tenant", "user", "client_name", "tool", "status", "result_count", "latency_ms")
    list_filter = ("status", "tool", "tenant")
    search_fields = ("user__email", "tenant__name", "client_name", "tool")
    date_hierarchy = "created_at"

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
