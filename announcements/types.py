from __future__ import annotations

import strawberry
import strawberry_django
from strawberry.relay import Node

from . import models


@strawberry_django.type(models.Announcement)
class Announcement(Node):
    uuid: str
    title: str
    body: str
    audience: str
    published_at: str | None
    created_at: str
    updated_at: str
    tenant_id: strawberry.ID

    @strawberry.field
    def created_by_name(self) -> str:
        """Display name of the admin who posted it — shown as the
        author byline in the mobile feed. Falls back to email, then
        'Spark team'."""
        u = getattr(self, "created_by", None)
        if u is None:
            return "Spark team"
        full = " ".join(
            x
            for x in [
                getattr(u, "first_name", "") or "",
                getattr(u, "last_name", "") or "",
            ]
            if x
        ).strip()
        return full or (getattr(u, "email", "") or "") or "Spark team"


@strawberry.type
class AnnouncementResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    announcement: Announcement | None = None
