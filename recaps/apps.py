from django.apps import AppConfig


class RecapsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'recaps'

    def ready(self):
        """Import signals when the app is ready."""
        import recaps.signals  # noqa
