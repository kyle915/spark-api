from django.apps import AppConfig


class PhotoLibraryConfig(AppConfig):
    """Searchable recap photos: tags stored once per photo, and the search
    over them. Its own app so its tables never race other work for a
    migration number in ``recaps``."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "photo_library"
    verbose_name = "Photo library"
