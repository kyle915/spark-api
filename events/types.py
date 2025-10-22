import strawberry_django
from strawberry import auto

from . import models

@strawberry_django.type(models.EventType)
class EventType:
    id: auto
    uuid: auto
    name: auto
    created_at: auto