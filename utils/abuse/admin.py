from django.contrib import admin

from .models import AmbassadorReadLog, AuditLogEntry, RateLimitCounter, UsageBucket


@admin.register(AuditLogEntry)
class AuditLogEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "operation", "user_email", "tenant_id",
                    "row_count", "success", "ip", "is_admin")
    list_filter = ("kind", "success", "is_admin", "operation")
    search_fields = ("user_email", "operation", "ip", "user_agent")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AuditLogEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(UsageBucket)
class UsageBucketAdmin(admin.ModelAdmin):
    list_display = ("hour", "user_id", "tenant_id", "operation", "request_count",
                    "paged_count", "throttled_count", "error_count", "response_bytes", "is_admin")
    list_filter = ("is_admin",)
    search_fields = ("operation", "user__email")
    date_hierarchy = "hour"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AmbassadorReadLog)
class AmbassadorReadLogAdmin(admin.ModelAdmin):
    list_display = ("day", "user_id", "tenant_id", "ambassador_key", "first_operation")
    search_fields = ("user__email", "ambassador_key")
    date_hierarchy = "day"

    def has_add_permission(self, request):
        return False


@admin.register(RateLimitCounter)
class RateLimitCounterAdmin(admin.ModelAdmin):
    list_display = ("key", "window_start", "count")
    search_fields = ("key",)
