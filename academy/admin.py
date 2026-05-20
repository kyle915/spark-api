from django.contrib import admin

from .models import AcademyModule


@admin.register(AcademyModule)
class AcademyModuleAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "tenant", "published", "order", "updated_at")
    list_filter = ("tenant", "kind", "published")
    search_fields = ("title", "body")
    ordering = ("order", "-updated_at")
