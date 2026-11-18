from django.apps import AppConfig


class HoursCheckConfig(AppConfig):
    """Hours check before payroll: rule-based flags on padded or impossible
    shifts, shown beside the payroll approve buttons. Its own app so its one
    table never races other work for a migration number in ``events`` or
    ``ambassadors``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "hours_check"
    verbose_name = "Hours check"
