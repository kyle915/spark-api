from django.contrib import admin

from .models import CloseLink, SalesAssistDraft, SalesAssistRun, SalesProfile


@admin.register(CloseLink)
class CloseLinkAdmin(admin.ModelAdmin):
    list_display = ("tenant", "close_lead_id", "close_opportunity_id", "renewal_date", "active")
    search_fields = ("tenant__name", "close_lead_id")
    raw_id_fields = ("tenant", "linked_by")


@admin.register(SalesProfile)
class SalesProfileAdmin(admin.ModelAdmin):
    list_display = ("tenant", "category", "descriptor", "exclude_from_examples", "source")
    search_fields = ("tenant__name", "category", "descriptor")
    raw_id_fields = ("tenant",)


@admin.register(SalesAssistDraft)
class SalesAssistDraftAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "status", "lead_name", "tenant", "period_start", "reason")
    list_filter = ("kind", "status")
    search_fields = ("lead_name", "close_lead_id", "idem_key")
    raw_id_fields = ("tenant", "run")
    readonly_fields = [f.name for f in SalesAssistDraft._meta.fields]


@admin.register(SalesAssistRun)
class SalesAssistRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "action", "applied", "ai_calls", "est_cost_usd")
    readonly_fields = [f.name for f in SalesAssistRun._meta.fields]
