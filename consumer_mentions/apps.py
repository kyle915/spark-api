from django.apps import AppConfig


class ConsumerMentionsConfig(AppConfig):
    """Per-recap tags on consumer comments (themes and other brands), and the
    month-over-month card built from them. Its own app so its tables never
    race other work for a migration number in ``recaps`` or ``tenants``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "consumer_mentions"
    verbose_name = "Consumer mentions"
