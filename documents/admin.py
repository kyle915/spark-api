from django.contrib import admin

from .models import AmbassadorDocument


@admin.register(AmbassadorDocument)
class AmbassadorDocumentAdmin(admin.ModelAdmin):
    list_display = ("uuid", "ambassador", "doc_type", "expires_on", "status", "created_at")
    list_filter = ("doc_type", "status")
    search_fields = ("uuid", "title", "ambassador__user__email")
