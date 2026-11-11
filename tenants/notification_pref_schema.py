"""GraphQL for a person's own email preferences (clients schema).

Read and write only ever touch the CALLER's rows. See
``utils.notification_prefs`` for the categories and how sending honours them.
"""

from __future__ import annotations

import logging

import strawberry
from asgiref.sync import sync_to_async

from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.permissions import StrictIsAuthenticated
from utils.notification_prefs import CATEGORIES, CATEGORY_BY_KEY

logger = logging.getLogger(__name__)


@strawberry.type
class NotificationPreferenceItem:
    category: str
    label: str
    description: str
    email_enabled: bool


@strawberry.input
class SetMyNotificationPreferenceInput(SparkGraphQLInput):
    category: str
    email_enabled: bool


@strawberry.type
class SetMyNotificationPreferenceResponse:
    success: bool
    message: str
    category: str
    # Authoritative only when ``success`` is true.
    email_enabled: bool
    client_mutation_id: strawberry.ID | None = None


def _request_user(info: strawberry.Info):
    user = getattr(info.context.request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user


@strawberry.type
class NotificationPreferenceQueries:
    @strawberry.field(permission_classes=[StrictIsAuthenticated])
    async def my_notification_preferences(
        self, info: strawberry.Info
    ) -> list[NotificationPreferenceItem]:
        """Every optional email category with the caller's current choice.
        Unset means on. Never raises; on a read failure everything shows on,
        which matches what would actually be sent."""
        user = _request_user(info)

        def _read() -> dict[str, bool]:
            from tenants.models import NotificationPreference

            return dict(
                NotificationPreference.objects.filter(user_id=user.pk).values_list(
                    "category", "email_enabled"
                )
            )

        chosen: dict[str, bool] = {}
        if user is not None:
            try:
                chosen = await sync_to_async(_read, thread_sensitive=True)()
            except Exception as exc:  # noqa: BLE001
                logger.warning("myNotificationPreferences read failed: %s", exc)
        return [
            NotificationPreferenceItem(
                category=c.key,
                label=c.label,
                description=c.description,
                email_enabled=bool(chosen.get(c.key, True)),
            )
            for c in CATEGORIES
        ]


@strawberry.type
class NotificationPreferenceMutations:
    @strawberry.mutation(permission_classes=[StrictIsAuthenticated])
    async def set_my_notification_preference(
        self,
        info: strawberry.Info,
        input: SetMyNotificationPreferenceInput,
    ) -> SetMyNotificationPreferenceResponse:
        """Turn one optional email category on or off for the caller only."""
        key = (input.category or "").strip()
        desired = bool(input.email_enabled)

        def _fail(message: str) -> SetMyNotificationPreferenceResponse:
            return SetMyNotificationPreferenceResponse(
                success=False,
                message=message,
                category=key,
                email_enabled=desired,
                client_mutation_id=input.client_mutation_id,
            )

        user = _request_user(info)
        if user is None:
            return _fail("Sign in to change your email preferences.")
        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return _fail("Unknown email category.")

        def _write() -> bool:
            from tenants.models import NotificationPreference

            pref, _created = NotificationPreference.objects.get_or_create(
                user_id=user.pk,
                category=key,
                defaults={"email_enabled": desired},
            )
            if pref.email_enabled != desired:
                pref.email_enabled = desired
                pref.save(update_fields=["email_enabled", "updated_at"])
            return pref.email_enabled

        try:
            state = await sync_to_async(_write, thread_sensitive=True)()
        except Exception as exc:  # noqa: BLE001
            logger.exception("setMyNotificationPreference failed: %s", exc)
            return _fail("Could not update your email preferences.")

        return SetMyNotificationPreferenceResponse(
            success=True,
            message=(
                f"{category.label}: emails on."
                if state
                else f"{category.label}: emails off."
            ),
            category=key,
            email_enabled=state,
            client_mutation_id=input.client_mutation_id,
        )
