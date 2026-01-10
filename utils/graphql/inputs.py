import strawberry
from typing import List, Dict, Any


@strawberry.input
class SparkGraphQLInput:
    """Base class for Spark GraphQL inputs."""

    client_mutation_id: strawberry.ID | None = None

    def to_dict(self, exclude: List[str] | None = None) -> Dict[str, Any]:
        """
        Convert Strawberry input to dictionary for Django model assignment.

        Args:
            exclude: Field names to exclude from dict

        Returns:
            Dictionary with field names and values
        """
        exclude: List[str] = exclude or []
        result: Dict[str, Any] = {}

        # Strawberry inputs store fields in __dict__
        for key, value in self.__dict__.items():
            if key in exclude or key == "client_mutation_id" or value is None:
                continue
            result[key] = value

        return result


@strawberry.input
class BaseTenantInput(SparkGraphQLInput):
    tenant_id: strawberry.ID | None = None


@strawberry.input
class BaseNameableInput(BaseTenantInput):
    name: str
