import strawberry


@strawberry.input
class TenantFiltersInput:
    name: str | None = None
    request_url_name: str | None = None
