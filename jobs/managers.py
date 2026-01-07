from django.db import models
from utils.models import BaseManager


class StatusManager(BaseManager, models.Manager):
    """Manager for Status model with async support."""

    pass


class JobManager(BaseManager, models.Manager):
    """Manager for Job model with async support."""

    pass


class AmbassadorJobManager(BaseManager, models.Manager):
    """Manager for AmbassadorJob model with async support."""

    pass

