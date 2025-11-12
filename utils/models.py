from django.db import transaction


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
