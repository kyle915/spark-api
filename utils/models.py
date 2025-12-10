from django.db import transaction
from asgiref.sync import sync_to_async


class WithDefaultAttribute:
    """
    Base class for models that have a default attribute.
    """

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)

            if not hasattr(self, 'is_default'):
                raise ValueError(
                    """is_default attribute not found. 
                    Please ensure the model has the is_default attribute. or
                    remove the WithDefaultAttribute mixin."""
                )

            # Set the default status to false if the current status is set to true
            if self.is_default:
                (
                    self.__class__.objects.filter(is_default=True)
                    .exclude(pk=self.pk)
                    .update(is_default=False)
                )


class BaseManager:
    """Base manager class."""

    def _get(self, *args, **kwargs):
        """Get the model."""
        return sync_to_async(self.get)(*args, **kwargs)

    def _filter(self, *args, **kwargs):
        """Filter the model."""
        return sync_to_async(self.filter)(*args, **kwargs)

    def _exists(self, *args, **kwargs):
        """Check if the model exists."""
        return sync_to_async(self.exists)(*args, **kwargs)

    def _create(self, *args, **kwargs):
        """Create the model."""
        return sync_to_async(self.create)(*args, **kwargs)


class Asyncable:
    """Make regular methods async."""

    def _save(self, *args, **kwargs):
        """Save the model."""
        return sync_to_async(self.save)(*args, **kwargs)

    def _delete(self):
        """Delete the model."""
        return sync_to_async(self.delete)()
