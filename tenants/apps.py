from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tenants'

    def ready(self):
        """Import signals when app is ready."""
        import tenants.dashboard.signals  # noqa: F401
