import strawberry

from utils.graphql.inputs import SparkGraphQLInput


@strawberry.input
class AvailabilitySlotInput:
    """One recurring weekly window. weekday 0=Mon..6=Sun; times are
    'HH:MM' (24h) wall-clock strings."""

    weekday: int
    start_time: str
    end_time: str
    note: str | None = None


@strawberry.input
class SetAvailabilityInput(SparkGraphQLInput):
    """Replace the calling BA's full recurring-availability set with
    `slots`. Passing an empty list clears all recurring slots (same as
    clearAvailability)."""

    slots: list[AvailabilitySlotInput]
