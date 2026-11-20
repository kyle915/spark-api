from django.apps import AppConfig


class AbuseConfig(AppConfig):
    name = "utils.abuse"
    label = "abuse"
    verbose_name = "Abuse guardrails (rate limits, audit log, usage)"
    default_auto_field = "django.db.models.BigAutoField"
