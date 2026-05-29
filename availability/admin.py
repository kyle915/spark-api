from django.contrib import admin

from .models import AmbassadorAvailability


@admin.register(AmbassadorAvailability)
class AmbassadorAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("ambassador", "is_recurring", "weekday", "date", "start_time", "end_time")
    list_filter = ("is_recurring", "weekday")
    search_fields = ("ambassador__user__email",)
