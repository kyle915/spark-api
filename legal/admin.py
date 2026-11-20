import csv

from django.contrib import admin
from django.http import HttpResponse

from .models import LegalAcceptance


@admin.action(description="Export selected acceptances as CSV")
def export_csv(modeladmin, request, queryset):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="legal-acceptances.csv"'
    writer = csv.writer(response)
    writer.writerow(
        ["email", "role", "document", "version", "accepted_at", "ip_address", "user_agent", "method"]
    )
    for row in queryset.order_by("email", "document", "-accepted_at"):
        writer.writerow(
            [row.email, row.role, row.document, row.version, row.accepted_at.isoformat(),
             row.ip_address, row.user_agent, row.method]
        )
    return response


@admin.register(LegalAcceptance)
class LegalAcceptanceAdmin(admin.ModelAdmin):
    """Read-only audit trail."""

    list_display = ("accepted_at", "email", "role", "document", "version", "ip_address", "method")
    list_filter = ("document", "version", "role", "method")
    search_fields = ("email", "user__email", "ip_address")
    date_hierarchy = "accepted_at"
    raw_id_fields = ("user",)
    actions = [export_csv]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
