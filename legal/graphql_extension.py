"""Server-side Terms gate for the clients + spark GraphQL schemas.

An admin or client user who has not accepted the current legal documents can
only run the calls the web clickwrap needs (legal status / accept, ``me``,
token refresh, health). Anything else returns ONE error and no data:

    {"message": "...", "extensions": {"code": "LEGAL_ACCEPTANCE_REQUIRED",
                                      "version": "2026-11-17"}}

The web app keys on that code to show the clickwrap, so the gate can't be
skipped by calling the API directly.

Not affected: anonymous calls (public pages, sign-in), ambassadors (mobile
schema / web check-in REST are separate endpoints and BAs aren't gated),
the MCP server and cron/task endpoints (they don't go through these schemas).
``LEGAL_ACCEPTANCE_ENFORCED=false`` turns enforcement off (tests default it
off; prod defaults on).
"""

from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from graphql import (
    ExecutionResult as GraphQLExecutionResult,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    InlineFragmentNode,
    OperationDefinitionNode,
)
from strawberry.extensions import SchemaExtension

from . import versions
from .service import needs_acceptance

logger = logging.getLogger("spark.legal")

ERROR_CODE = "LEGAL_ACCEPTANCE_REQUIRED"
ERROR_MESSAGE = "Please review and accept the Spark Terms of Service to continue."

# Root fields a not-yet-accepted admin/client user may still call.
ALLOWED_ROOT_FIELDS = frozenset(
    {
        "__typename",
        "__schema",
        "__type",
        # the clickwrap itself
        "myLegalStatus",
        "acceptLegalTerms",
        # basic profile / session
        "me",
        "myPreferences",
        "healthcheck",
        "serverInfo",
        "tenantPublic",
        "tokenAuth",
        "refreshToken",
        "verifyToken",
        "verifyAccount",
        "revokeToken",
        # standalone self-service workspace onboarding
        "startSelfServeOnboarding",
        "selfServeStarterCatalog",
    }
)


def _operation(document, operation_name):
    ops = [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]
    if operation_name:
        for op in ops:
            if op.name and op.name.value == operation_name:
                return op
        return None
    return ops[0] if len(ops) == 1 else None


def _root_field_names(op, document) -> set[str]:
    fragments = {
        d.name.value: d
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }
    names: set[str] = set()

    def collect(selection_set, seen):
        for sel in selection_set.selections:
            if isinstance(sel, FieldNode):
                names.add(sel.name.value)
            elif isinstance(sel, InlineFragmentNode):
                collect(sel.selection_set, seen)
            elif isinstance(sel, FragmentSpreadNode):
                name = sel.name.value
                if name not in seen and name in fragments:
                    seen.add(name)
                    collect(fragments[name].selection_set, seen)

    collect(op.selection_set, set())
    return names


def legal_required_error() -> GraphQLError:
    return GraphQLError(
        ERROR_MESSAGE,
        extensions={"code": ERROR_CODE, "version": versions.current_version()},
    )


class LegalAcceptanceGate(SchemaExtension):
    async def on_execute(self):
        if not getattr(settings, "LEGAL_ACCEPTANCE_ENFORCED", True):
            yield
            return
        ctx = self.execution_context
        request = getattr(ctx.context, "request", None)
        if request is None and isinstance(ctx.context, dict):
            request = ctx.context.get("request")
        user = getattr(request, "user", None)
        user_id = getattr(user, "pk", None)
        if user_id is None or not getattr(user, "is_authenticated", False):
            yield
            return

        document = ctx.graphql_document
        op = _operation(document, ctx.operation_name) if document is not None else None
        if op is None:
            yield
            return
        roots = _root_field_names(op, document)
        if roots and roots <= ALLOWED_ROOT_FIELDS:
            yield
            return

        try:
            blocked = await sync_to_async(needs_acceptance, thread_sensitive=True)(user_id)
        except Exception:  # noqa: BLE001 - fail open; the real call would hit the same DB
            logger.warning("legal acceptance check failed — not gating", exc_info=True)
            blocked = False

        if blocked:
            # Short-circuit WITHOUT raising out of the hook: strawberry skips
            # execution when a result is already set.
            ctx.result = GraphQLExecutionResult(data=None, errors=[legal_required_error()])
        yield
