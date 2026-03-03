"""
GraphQL validation helpers.

Shared functions for parsing/validating input that raise GraphQLError
with consistent messages.
"""
from datetime import date
from typing import Any

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


def validate_consumer_engagements_counts(
    *,
    total_consumer: int,
    first_time_consumers: int,
    brand_aware_consumers: int,
    willing_to_purchase_consumers: int,
    not_willing_consumers: int,
) -> None:
    """
    Validate that consumer engagement sub-counts are consistent with total_consumer.

    Raises GraphQLError when any of the following are true:
    - Any value is negative
    - Any sub-count exceeds total_consumer
    - willing_to_purchase_consumers + not_willing_consumers exceeds total_consumer
    """
    values: dict[str, Any] = {
        "total_consumer": total_consumer,
        "first_time_consumers": first_time_consumers,
        "brand_aware_consumers": brand_aware_consumers,
        "willing_to_purchase_consumers": willing_to_purchase_consumers,
        "not_willing_consumers": not_willing_consumers,
    }

    for field, value in values.items():
        if value < 0:
            raise GraphQLError(f"{field} cannot be negative.")

    if first_time_consumers > total_consumer:
        raise GraphQLError(
            "first_time_consumers cannot be greater than total_consumer."
        )
    if brand_aware_consumers > total_consumer:
        raise GraphQLError(
            "brand_aware_consumers cannot be greater than total_consumer."
        )
    if willing_to_purchase_consumers > total_consumer:
        raise GraphQLError(
            "willing_to_purchase_consumers cannot be greater than total_consumer."
        )
    if not_willing_consumers > total_consumer:
        raise GraphQLError(
            "not_willing_consumers cannot be greater than total_consumer."
        )

    if (
        willing_to_purchase_consumers + not_willing_consumers
        > total_consumer
    ):
        raise GraphQLError(
            "Sum of willing_to_purchase_consumers and "
            "not_willing_consumers cannot be greater than total_consumer."
        )
