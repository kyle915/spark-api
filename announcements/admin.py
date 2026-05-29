from django.contrib import admin

from .models import Announcement


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("uuid", "title", "tenant", "audience", "published_at", "created_by", "created_at")
    list_filter = ("audience",)
    search_fields = ("uuid", "title", "body", "tenant__name")
