import strawberry


@strawberry.type
class AvailabilitySlot:
    """One recurring weekly availability window for the calling BA.

    Times are ISO 'HH:MM' wall-clock strings. `weekday` is 0=Mon..6=Sun.
    """

    uuid: str
    weekday: int
    start_time: str  # "HH:MM"
    end_time: str    # "HH:MM"
    note: str | None = None


@strawberry.type
class SetAvailabilityResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    slots: list[AvailabilitySlot] | None = None


@strawberry.type
class ClearAvailabilityResponse:
    success: bool
    message: str
    client_mutation_id: strawberry.ID | None = None
    cleared_count: int = 0
