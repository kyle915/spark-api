import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.permissions import StrictIsAuthenticated
from ambassadors.models import Ambassador
from . import models, types


@strawberry.type
class DocumentMobileQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_documents(
        self,
        info: strawberry.Info,
        include_archived: bool = False,
    ) -> list[types.AmbassadorDocument]:
        """All documents owned by the calling BA, newest first."""
        user = info.context.request.user

        @sync_to_async
        def _load():
            ambassador = Ambassador.objects.filter(user=user).first()
            if not ambassador:
                return []
            qs = models.AmbassadorDocument.objects.filter(ambassador=ambassador)
            if not include_archived:
                qs = qs.exclude(status=models.DocumentStatus.ARCHIVED)
            return list(qs)

        return await _load()
