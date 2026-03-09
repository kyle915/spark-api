"""
GraphQL validation helpers.

Shared functions for parsing/validating input that raise GraphQLError
with consistent messages.
"""
from datetime import date

from graphql import GraphQLError

ISO_DATE_MESSAGE = "Use YYYY-MM-DD."


def parse_iso_date_optional(
    value: str | None,
    field_name: str = "date",
) -> date | None:
    """
    Parse an optional ISO date string (YYYY-MM-DD).

    Args:
        value: The string to parse, or None.
        field_name: Used in error message (e.g. "fromDate", "toDate").

    Returns:
        Parsed date, or None if value is None.

    Raises:
        GraphQLError: If value is not None and not a valid ISO date.
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise GraphQLError(
            f"Invalid {field_name} format: {value}. {ISO_DATE_MESSAGE}"
        )


def clamp_percentage(value: float) -> float:
    """
    Clamp a numeric value to the percentage range [0, 100].

    The caller is responsible for rounding if needed.
    """
    return max(0.0, min(value, 100.0))
