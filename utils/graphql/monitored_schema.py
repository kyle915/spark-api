"""JwtSchema subclass that feeds UNEXPECTED resolver crashes to the error
monitor.

GraphQL resolver exceptions never reach Django's exception machinery — they
are masked into the response's ``errors`` list, so a crashing mutation looks
like a 200 to every log line we had. strawberry's default ``process_errors``
logs every error (including expected business denials like "Event not
found."), which would flood the monitor with noise. This override splits
them:

- expected: the resolver deliberately raised GraphQLError → INFO log only.
- unexpected: any other exception type escaped a resolver → ERROR log with
  exc_info, which the ErrorEventLogHandler turns into a BackendErrorEvent +
  throttled alert email.
"""

from __future__ import annotations

import logging

from graphql import GraphQLError
from gqlauth.core.middlewares import JwtSchema

logger = logging.getLogger("spark.graphql")
_expected_logger = logging.getLogger("spark.graphql.expected")


class MonitoredJwtSchema(JwtSchema):
    def process_errors(self, errors, execution_context=None) -> None:
        for error in errors or []:
            original = getattr(error, "original_error", None)
            if original is None or isinstance(original, GraphQLError):
                # Query validation problems / deliberate business denials —
                # normal traffic, not incidents.
                _expected_logger.info("GraphQL error: %s", error)
                continue
            logger.error(
                "Unhandled resolver exception at %s: %s",
                getattr(error, "path", None),
                error.message,
                exc_info=(type(original), original, original.__traceback__),
            )
