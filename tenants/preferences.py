"""Per-user Settings preferences — GraphQL query + mutation.

Server-side home for the prefs the web Settings page (``SparkSettings.tsx``)
used to keep only in ``localStorage`` (``@spark.settings.*``), so they now
follow the user across devices. Backed by :class:`tenants.models.UserPreference`
(one row per user; a single free-form ``prefs`` JSON blob).

Exposed on the clients schema via two mixins that are merged into
``QueryClients`` / ``MutationClients`` in :mod:`tenants.schema`:

* ``myPreferences``    — the current user's prefs (defaults when none saved).
* ``setMyPreferences`` — upsert (partial-merge) the current user's prefs.

Both are scoped to the authenticated user (``info.context.request.user``) and
NEVER raise: reads fall back to defaults, writes return a safe
``success=False`` response on any error. This keeps the Settings page robust
even if a request arrives unauthenticated or the DB hiccups.
"""

import strawberry
from strawberry import relay
from strawberry.scalars import JSON
from asgiref.sync import sync_to_async

from utils.graphql.inputs import SparkGraphQLInput
from .models import UserPreference


@strawberry.type
class UserPreferencesType:
    """The authenticated user's resolved Settings preferences.

    ``prefs`` is the stored JSON merged over
    :attr:`UserPreference.DEFAULT_PREFS`, so every documented key is always
    present even for a user who has never saved. Keys mirrored today:
    ``timezone`` (str), ``currency`` (str), ``activations`` (map of
    activation-type id -> bool).
    """

    prefs: JSON


def _defaults_response() -> UserPreferencesType:
    """Baseline prefs for an unsaved / unauthenticated read."""
    return UserPreferencesType(prefs=dict(UserPreference.DEFAULT_PREFS))


@strawberry.type
class SetMyPreferencesResponse:
    success: bool
    message: str
    preferences: UserPreferencesType | None = None
    client_mutation_id: strawberry.ID | None = None


@strawberry.input
class SetMyPreferencesInput(SparkGraphQLInput):
    """Partial update for the current user's Settings preferences.

    ``prefs`` is shallow-merged over what is already stored, so callers can
    send only the keys that changed (e.g. just ``{"timezone": "..."}``).
    Omitting it (or sending null) is a no-op upsert that simply returns the
    current resolved prefs.
    """

    prefs: JSON | None = None


@strawberry.type
class MyPreferencesQueries:
    @strawberry.field
    async def my_preferences(self, info: strawberry.Info) -> UserPreferencesType:
        """The current user's Settings preferences (defaults when unsaved).

        Scoped to ``info.context.request.user``. Never raises: returns the
        baseline defaults for an unauthenticated request or when no row
        exists yet, so the Settings page always has something to render.
        """
        user = info.context.request.user
        if not user or not user.is_authenticated:
            return _defaults_response()

        @sync_to_async
        def _load() -> UserPreferencesType:
            pref = UserPreference.objects.filter(user=user).first()
            if pref is None:
                return _defaults_response()
            return UserPreferencesType(prefs=pref.merged())

        try:
            return await _load()
        except Exception:
            return _defaults_response()


@strawberry.type
class MyPreferencesMutations:
    @relay.mutation
    async def set_my_preferences(
        self,
        info: strawberry.Info,
        input: SetMyPreferencesInput,
    ) -> SetMyPreferencesResponse:
        """Upsert the current user's Settings preferences (partial-merge).

        Creates the user's :class:`UserPreference` row on first save and
        shallow-merges ``input.prefs`` over whatever is stored, so the
        Settings page can persist one toggle at a time without clobbering
        the rest. Scoped to ``info.context.request.user``.

        Never raises: returns ``success=False`` with a message on an
        unauthenticated request or a DB error, and echoes the resolved prefs
        on success so the client can reconcile immediately.
        """
        user = info.context.request.user
        if not user or not user.is_authenticated:
            return SetMyPreferencesResponse(
                success=False,
                message="User not authenticated.",
                client_mutation_id=input.client_mutation_id,
            )

        incoming = input.prefs
        if incoming is not None and not isinstance(incoming, dict):
            return SetMyPreferencesResponse(
                success=False,
                message="prefs must be an object.",
                client_mutation_id=input.client_mutation_id,
            )

        @sync_to_async
        def _save() -> UserPreferencesType:
            pref, _ = UserPreference.objects.get_or_create(user=user)
            if incoming:
                current = pref.prefs if isinstance(pref.prefs, dict) else {}
                current.update(incoming)
                pref.prefs = current
                pref.save(update_fields=["prefs", "updated_at"])
            return UserPreferencesType(prefs=pref.merged())

        try:
            resolved = await _save()
        except Exception as exc:
            return SetMyPreferencesResponse(
                success=False,
                message=f"Could not save preferences: {exc}",
                client_mutation_id=input.client_mutation_id,
            )

        return SetMyPreferencesResponse(
            success=True,
            message="Preferences saved.",
            preferences=resolved,
            client_mutation_id=input.client_mutation_id,
        )
