"""
Dashboard GraphQL schema.

This module exports dashboard queries for integration with Client and Spark schemas.
"""
import strawberry

from . import queries


@strawberry.type
class DashboardQueries(
    queries.DashboardQueries
):
    """Dashboard queries for client dashboards."""
    pass
