import strawberry
from typing import List, Dict, Any


class SparkGraphQLInput:
    """Base class for Spark GraphQL inputs."""

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
            if key in exclude or value is None:
                continue
            result[key] = value

        return result
