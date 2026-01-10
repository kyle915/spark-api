"""
Helper functions for GraphQL testing.

This module provides utilities for executing GraphQL mutations and queries
in tests using strawberry's test client.
"""
from typing import Any, Dict, Optional
from strawberry.django.test import GraphQLTestClient
from django.test import Client
from django.contrib.auth import get_user_model
from gqlauth.core.utils import get_token

User = get_user_model()


class GraphQLTestHelper:
    """
    Helper class for executing GraphQL operations in tests.
    """

    def __init__(self, schema, client: Optional[Client] = None):
        """
        Initialize the GraphQL test helper.

        Args:
            schema: Strawberry GraphQL schema instance
            client: Optional Django test client (creates new one if not provided)
        """
        self.schema = schema
        self.client = client or Client()
        self.graphql_client = GraphQLTestClient(self.schema, self.client)

    def execute_mutation(
        self,
        mutation: str,
        variables: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a GraphQL mutation.

        Args:
            mutation: GraphQL mutation string
            variables: Optional variables dictionary
            headers: Optional HTTP headers (e.g., for authentication)

        Returns:
            dict: Response data from the mutation
        """
        if headers:
            for key, value in headers.items():
                self.client.defaults[key] = value

        response = self.graphql_client.query(
            mutation,
            variables=variables or {},
        )

        return response

    def execute_query(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a GraphQL query.

        Args:
            query: GraphQL query string
            variables: Optional variables dictionary
            headers: Optional HTTP headers (e.g., for authentication)

        Returns:
            dict: Response data from the query
        """
        if headers:
            for key, value in headers.items():
                self.client.defaults[key] = value

        response = self.graphql_client.query(
            query,
            variables=variables or {},
        )

        return response


def get_auth_headers(user: User) -> Dict[str, str]:
    """
    Generate JWT authentication headers for a user.

    Args:
        user: User instance to generate token for

    Returns:
        dict: Headers dictionary with Authorization header
    """
    token = get_token(user, "authentication")
    return {
        'HTTP_AUTHORIZATION': f'Bearer {token}',
    }


async def execute_graphql_mutation_async(
    schema,
    mutation: str,
    variables: Optional[Dict[str, Any]] = None,
    context_value: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Execute a GraphQL mutation asynchronously.

    This is useful for testing async mutations directly without going through
    the HTTP layer.

    Args:
        schema: Strawberry GraphQL schema instance
        mutation: GraphQL mutation string
        variables: Optional variables dictionary
        context_value: Optional context value (e.g., request with user)

    Returns:
        dict: Response data from the mutation
    """
    from strawberry import execute_sync

    result = execute_sync(
        schema,
        mutation,
        variable_values=variables or {},
        context_value=context_value or {},
    )

    return result.data if result.data else {}
