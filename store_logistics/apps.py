from django.apps import AppConfig


class StoreLogisticsConfig(AppConfig):
    """Persistent door/store logistics notes surfaced on Store Profile and
    shift briefings. Isolated in its own app so its migration leaf never
    collides with concurrent work in ``events`` or ``recaps``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "store_logistics"
    verbose_name = "Store logistics"
