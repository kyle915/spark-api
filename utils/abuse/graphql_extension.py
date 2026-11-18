"""Strawberry schema extensions: rate limits, complexity limits, introspection
block, brand page-size cap, export/bulk caps + audit, usage logging.

Wire with :func:`guardrail_extensions` (see config/schema_client.py)::

    extensions=[DjangoOptimizerExtension, BlockIntrospectionForAnonymous,
                *guardrail_extensions()]

Per request cost: identity lookup (cached 60s per user), ONE limiter upsert
round trip before execution, ONE usage upsert after (plus an audit row only
for export/bulk/download/throttle events and an insert-ignore for BA reads).

Errors are GraphQL errors with ``extensions.code`` the FE keys on:
RATE_LIMITED, EXPORT_LIMIT, BULK_LIMIT, LOGIN_THROTTLED, QUERY_TOO_COMPLEX,
INTROSPECTION_DISABLED (never the "introspection is disabled" wording for
signed-in users — the FE treats that phrase as an expired token).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from asgiref.sync import sync_to_async
from django.conf import settings
from graphql import (
    ExecutionResult as GraphQLExecutionResult,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    InlineFragmentNode,
    IntValueNode,
    OperationDefinitionNode,
    VariableNode,
    get_named_type,
    parse,
)
from graphql.utilities import value_from_ast_untyped
from strawberry.extensions import (
    MaxAliasesLimiter,
    MaxTokensLimiter,
    QueryDepthLimiter,
    SchemaExtension,
)

from . import policy
from .audit import record_audit, summarize_args
from .identity import ANONYMOUS, Identity, client_ip, resolve_identity, user_agent
from .limiter import RateLimitExceeded, hit_many
from .usage import record_ambassador_reads, record_usage

logger = logging.getLogger("spark.abuse")

_COMPLEXITY_MARKERS = (
    "exceeds maximum operation depth",
    "aliases found",
    "tokens. Parsing aborted",
)


# --------------------------------------------------------------------------
# document helpers
# --------------------------------------------------------------------------

def _operation(document, operation_name: str | None) -> OperationDefinitionNode | None:
    ops = [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]
    if operation_name:
        for op in ops:
            if op.name and op.name.value == operation_name:
                return op
        return None
    return ops[0] if len(ops) == 1 else None


def _fragments(document) -> dict[str, FragmentDefinitionNode]:
    return {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}


def _iter_fields(selection_set, fragments, _seen=None) -> Iterable[FieldNode]:
    """Every FieldNode reachable from ``selection_set`` (fragments inlined)."""
    if selection_set is None:
        return
    _seen = _seen if _seen is not None else set()
    for sel in selection_set.selections:
        if isinstance(sel, FieldNode):
            yield sel
            yield from _iter_fields(sel.selection_set, fragments, _seen)
        elif isinstance(sel, InlineFragmentNode):
            yield from _iter_fields(sel.selection_set, fragments, _seen)
        elif isinstance(sel, FragmentSpreadNode):
            name = sel.name.value
            if name in _seen or name not in fragments:
                continue
            _seen.add(name)
            yield from _iter_fields(fragments[name].selection_set, fragments, _seen)


def _root_fields(op, fragments) -> list[FieldNode]:
    out: list[FieldNode] = []

    def collect(selection_set, seen):
        for sel in selection_set.selections:
            if isinstance(sel, FieldNode):
                out.append(sel)
            elif isinstance(sel, InlineFragmentNode):
                collect(sel.selection_set, seen)
            elif isinstance(sel, FragmentSpreadNode):
                name = sel.name.value
                if name not in seen and name in fragments:
                    seen.add(name)
                    collect(fragments[name].selection_set, seen)

    collect(op.selection_set, set())
    return out


def _args(field: FieldNode, variables: dict | None) -> dict[str, Any]:
    out = {}
    for arg in field.arguments or ():
        try:
            out[arg.name.value] = value_from_ast_untyped(arg.value, variables or {})
        except Exception:  # noqa: BLE001 - defensive: never fail the request here
            out[arg.name.value] = None
    return out


def _max_list_len(value: Any) -> int:
    if isinstance(value, list):
        return max([len(value)] + [_max_list_len(v) for v in value[:50]])
    if isinstance(value, dict):
        return max([0] + [_max_list_len(v) for v in value.values()])
    return 0


def _find_identifier(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("email", "username", "identifier", "phone"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
        for v in value.values():
            found = _find_identifier(v)
            if found:
                return found
    return None


def _is_paged(args: dict) -> bool:
    if args.get("after") or args.get("before"):
        return True
    offset = args.get("offset")
    return isinstance(offset, int) and offset > 0


def _collect_ambassador_keys(schema, field: FieldNode, parent_type, data, fragments, out: set):
    """Walk a root field's result alongside its GraphQL type and collect BA keys."""

    def walk(selection_set, gtype, value):
        if value is None or selection_set is None:
            return
        if isinstance(value, list):
            for item in value[:5000]:
                walk(selection_set, gtype, item)
            return
        if not isinstance(value, dict):
            return
        named = get_named_type(gtype) if gtype is not None else None
        type_name = getattr(named, "name", None)
        if type_name in policy.AMBASSADOR_TYPES:
            key = value.get("uuid") or value.get("id")
            if key:
                out.add(str(key))
        fields = getattr(named, "fields", None) or {}
        for sel in selection_set.selections:
            if isinstance(sel, FieldNode):
                name = sel.name.value
                resp_key = sel.alias.value if sel.alias else name
                if resp_key not in value:
                    continue
                child = value[resp_key]
                if name in policy.AMBASSADOR_KEY_FIELDS and isinstance(child, (str, int)):
                    out.add(str(child))
                    continue
                if sel.selection_set is not None:
                    fdef = fields.get(name)
                    walk(sel.selection_set, fdef.type if fdef else None, child)
            elif isinstance(sel, InlineFragmentNode):
                cond = sel.type_condition.name.value if sel.type_condition else None
                walk(sel.selection_set, schema.get_type(cond) if cond else named, value)
            elif isinstance(sel, FragmentSpreadNode):
                frag = fragments.get(sel.name.value)
                if frag is not None:
                    walk(frag.selection_set, schema.get_type(frag.type_condition.name.value), value)

    fdef = parent_type.fields.get(field.name.value) if parent_type is not None else None
    walk(field.selection_set, fdef.type if fdef else None, data)


# --------------------------------------------------------------------------
# the extension
# --------------------------------------------------------------------------

class GuardrailsExtension(SchemaExtension):
    """See module docstring. ``schema_label`` namespaces the limiter keys."""

    schema_label = "clients"

    # ---- token limit ------------------------------------------------------
    def on_parse(self):
        # strawberry's async execute() ignores parse_options, so the stock
        # MaxTokensLimiter is a no-op on our AsyncGraphQLView. Parse here
        # with max_tokens; strawberry reuses the document we set.
        ctx = self.execution_context
        if ctx.graphql_document is None and ctx.query:
            try:
                ctx.graphql_document = parse(ctx.query, max_tokens=policy.limits().max_tokens)
            except GraphQLError as err:
                if "tokens" in (err.message or ""):
                    raise GraphQLError(
                        "This request is too large or complex.",
                        extensions={"code": "QUERY_TOO_COMPLEX"},
                    ) from err
                # syntax errors: let strawberry parse again and report them
        yield

    # ---- complexity error tagging --------------------------------------
    def on_operation(self):
        yield
        ctx = self.execution_context
        for err in list(ctx.pre_execution_errors or []):
            msg = getattr(err, "message", "") or ""
            if any(marker in msg for marker in _COMPLEXITY_MARKERS):
                ext = dict(err.extensions or {})
                ext.setdefault("code", "QUERY_TOO_COMPLEX")
                err.extensions = ext

    # ---- main hook -------------------------------------------------------
    async def on_execute(self):
        if not getattr(settings, "ABUSE_GUARDRAILS_ENABLED", True):
            yield
            return
        ctx = self.execution_context
        request = getattr(ctx.context, "request", None)
        if request is None and isinstance(ctx.context, dict):
            request = ctx.context.get("request")
        user = getattr(request, "user", None)
        try:
            identity = await sync_to_async(resolve_identity)(user)
        except Exception:  # noqa: BLE001 - fail open
            logger.warning("identity resolution failed — guardrails skipped", exc_info=True)
            identity = ANONYMOUS

        document = ctx.graphql_document
        op = _operation(document, ctx.operation_name) if document is not None else None
        if op is None:
            yield
            return
        fragments = _fragments(document)
        variables = ctx.variables if ctx.variables is not None else {}
        roots = _root_fields(op, fragments)
        root_names = {f.name.value for f in roots}
        ip = client_ip(request) if request is not None else "unknown"
        ua = user_agent(request) if request is not None else ""
        op_label = (ctx.operation_name or (sorted(root_names)[0] if root_names else "anonymous"))[:120]

        lim = policy.limits()
        root_args = {f.name.value: _args(f, variables) for f in roots}

        async def _preflight():
            # 1) introspection — staff/spark-admin only (DEBUG keeps old behaviour)
            if not settings.DEBUG and identity.is_authenticated and not identity.is_admin:
                if any(f.name.value in ("__schema", "__type") for f in _iter_fields(op.selection_set, fragments)):
                    raise GraphQLError(
                        "Schema introspection is not available.",
                        extensions={"code": "INTROSPECTION_DISABLED"},
                    )


            # 2) per-call item caps on bulk mutations
            max_items = lim.admin_bulk_max_items if identity.is_admin else lim.bulk_max_items
            for name in root_names & policy.BULK_FIELDS:
                size = _max_list_len(root_args.get(name))
                if max_items > 0 and size > max_items:
                    await sync_to_async(record_audit)(
                        "throttled", name, identity=identity,
                        params={"reason": "bulk_max_items", "items": size, "max": max_items},
                        success=False, ip=ip, ua=ua,
                    )
                    raise RateLimitExceeded(
                        f"Too many items in one request (max {max_items}).",
                        code="BULK_LIMIT", retry_after=1,
                    )

            # 3) rate limits — one round trip for every applicable rule
            rules, code_for_key = self._rules(identity, ip, root_names, root_args, lim)
            blocked = None
            if rules:
                blocked = await sync_to_async(hit_many)(rules)
            if blocked is None and identity.is_brand and root_names & policy.AMBASSADOR_LIST_FIELDS:
                if await sync_to_async(self._over_distinct_ambassadors)(identity, lim):
                    await sync_to_async(self._log_throttle)(identity, op_label, ip, ua,
                                                            "RATE_LIMITED:ambassador_distinct")
                    raise RateLimitExceeded(policy.THROTTLE_MESSAGES["RATE_LIMITED"],
                                            code="RATE_LIMITED", retry_after=3600)
            if blocked is not None:
                code = code_for_key.get(blocked.key, "RATE_LIMITED")
                await sync_to_async(self._log_throttle)(identity, op_label, ip, ua, code,
                                                        window=blocked.window_seconds, limit=blocked.limit)
                raise RateLimitExceeded(policy.THROTTLE_MESSAGES.get(code, policy.THROTTLE_MESSAGES["RATE_LIMITED"]),
                                        code=code, retry_after=blocked.retry_after)

            # 4) brand page-size cap (clamp, never reject)
            if identity.is_brand and lim.client_page_cap > 0:
                self._clamp_page_sizes(op, fragments, variables, lim.client_page_cap)

        try:
            await _preflight()
        except GraphQLError as err:
            # Short-circuit WITHOUT raising out of the hook: strawberry skips
            # execution when a result is already set, and every other
            # extension (e.g. the optimizer) unwinds normally.
            ctx.result = GraphQLExecutionResult(data=None, errors=[err])
            yield
            return

        yield

        # 5) after execution: usage, audit, BA reads (never raises)
        try:
            await sync_to_async(self._after)(identity, op_label, roots, root_args, ip, ua, fragments)
        except Exception:  # noqa: BLE001
            logger.warning("guardrail post-processing failed", exc_info=True)

    # ---- helpers ---------------------------------------------------------
    def _rules(self, identity: Identity, ip: str, root_names: set, root_args: dict, lim):
        p = self.schema_label
        rules: list[tuple[str, int, int]] = []
        codes: dict[str, str] = {}

        def add(key, limit, window, code="RATE_LIMITED"):
            rules.append((key, limit, window))
            codes[key[:200]] = code

        if identity.is_authenticated:
            uid = identity.user_id
            if identity.is_admin:
                add(f"{p}:gql:u{uid}:1m", lim.admin_per_minute, 60)
                add(f"{p}:gql:u{uid}:1h", lim.admin_per_hour, 3600)
            else:
                add(f"{p}:gql:u{uid}:1m", lim.user_per_minute, 60)
                add(f"{p}:gql:u{uid}:1h", lim.user_per_hour, 3600)
                if root_names & policy.AMBASSADOR_LIST_FIELDS:
                    add(f"{p}:amb:u{uid}:1m", lim.ambassador_per_minute, 60)
                    add(f"{p}:amb:u{uid}:1h", lim.ambassador_per_hour, 3600)
                    add(f"{p}:amb:u{uid}:1d", lim.ambassador_per_day, 86400)
            if root_names & policy.EXPORT_FIELDS:
                if identity.is_admin:
                    add(f"{p}:exp:u{uid}:1h", lim.admin_exports_per_hour, 3600, "EXPORT_LIMIT")
                else:
                    add(f"{p}:exp:u{uid}:1h", lim.exports_per_hour, 3600, "EXPORT_LIMIT")
                    add(f"{p}:exp:u{uid}:1d", lim.exports_per_day, 86400, "EXPORT_LIMIT")
                    if identity.tenant_id:
                        add(f"{p}:exp:t{identity.tenant_id}:1d", lim.tenant_exports_per_day,
                            86400, "EXPORT_LIMIT")
            if root_names & policy.BULK_FIELDS:
                add(f"{p}:bulk:u{uid}:1h",
                    lim.admin_bulk_per_hour if identity.is_admin else lim.bulk_per_hour,
                    3600, "BULK_LIMIT")
            if root_names & policy.DOWNLOAD_FIELDS and not identity.is_admin:
                add(f"{p}:dl:u{uid}:1h", lim.downloads_per_hour, 3600)
        else:
            add(f"{p}:gql:ip{ip}:1m", lim.anon_ip_per_minute, 60)
            add(f"{p}:gql:ip{ip}:1h", lim.anon_ip_per_hour, 3600)

        login = root_names & policy.LOGIN_FIELDS
        if login:
            add(f"login:ip{ip}:15m", lim.login_ip_per_15m, 900, "LOGIN_THROTTLED")
            for name in login:
                ident = _find_identifier(root_args.get(name))
                if ident:
                    add(f"login:id:{ident}:15m", lim.login_identifier_per_15m, 900,
                        "LOGIN_THROTTLED")
        return rules, codes

    @staticmethod
    def _over_distinct_ambassadors(identity: Identity, lim) -> bool:
        if lim.ambassador_distinct_per_day <= 0:
            return False
        from datetime import datetime, timezone

        from django.db import DatabaseError

        from .models import AmbassadorReadLog

        try:
            n = AmbassadorReadLog.objects.filter(
                day=datetime.now(timezone.utc).date(), user_id=identity.user_id
            ).count()
        except DatabaseError:
            logger.warning("distinct BA count failed — failing open", exc_info=True)
            return False
        return n >= lim.ambassador_distinct_per_day

    def _log_throttle(self, identity, op_label, ip, ua, code, window=None, limit=None):
        record_usage(identity, op_label, ip=ip, ua=ua, throttled=True)
        record_audit("throttled", op_label, identity=identity,
                     params={"code": code, "window": window, "limit": limit},
                     success=False, ip=ip, ua=ua)

    @staticmethod
    def _clamp_page_sizes(op, fragments, variables: dict, cap: int) -> None:
        exempt = set(getattr(settings, "ABUSE_PAGE_CAP_EXEMPT_FIELDS", ()) or ())
        for field in _iter_fields(op.selection_set, fragments):
            if field.name.value in exempt:
                continue
            for arg in field.arguments or ():
                if arg.name.value not in policy.PAGE_ARG_NAMES:
                    continue
                node = arg.value
                if isinstance(node, IntValueNode):
                    try:
                        if int(node.value) > cap:
                            node.value = str(cap)
                    except ValueError:
                        pass
                elif isinstance(node, VariableNode):
                    name = node.name.value
                    val = variables.get(name)
                    if isinstance(val, int) and not isinstance(val, bool) and val > cap:
                        variables[name] = cap

    def _after(self, identity, op_label, roots, root_args, ip, ua, fragments):
        ctx = self.execution_context
        result = ctx.result
        data = getattr(result, "data", None) or {}
        errors = getattr(result, "errors", None) or []
        error_paths = {str(e.path[0]) for e in errors if getattr(e, "path", None)}
        paged = any(_is_paged(a or {}) for a in root_args.values())

        size = 0
        if identity.is_brand and data:
            try:
                size = len(json.dumps(data, default=str))
            except (TypeError, ValueError):
                size = 0
        record_usage(identity, op_label, ip=ip, ua=ua, error=bool(errors), paged=paged,
                     response_bytes=size)

        gschema = ctx.schema._schema if hasattr(ctx.schema, "_schema") else None
        for field in roots:
            name = field.name.value
            resp_key = field.alias.value if field.alias else name
            kind = None
            if name in policy.EXPORT_FIELDS:
                kind = "export"
            elif name in policy.BULK_FIELDS:
                kind = "bulk"
            elif name in policy.DOWNLOAD_FIELDS:
                kind = "download_url"
            value = data.get(resp_key) if isinstance(data, dict) else None
            if kind:
                args = root_args.get(name) or {}
                success = resp_key not in error_paths and value is not None
                if isinstance(value, dict) and value.get("success") is False:
                    success = False
                record_audit(
                    kind, name, identity=identity, params=summarize_args(args),
                    row_count=self._row_count(args, value), success=success, ip=ip, ua=ua,
                )
            if (
                identity.is_brand
                and name in policy.AMBASSADOR_LOGGED_FIELDS
                and value is not None
                and gschema is not None
            ):
                keys: set[str] = set()
                parent = gschema.query_type if ctx.operation_type.value == "query" else gschema.mutation_type
                try:
                    _collect_ambassador_keys(gschema, field, parent, value, fragments, keys)
                except Exception:  # noqa: BLE001
                    logger.warning("BA key walk failed for %s", name, exc_info=True)
                if keys:
                    record_ambassador_reads(identity, keys, name)
                    if name in policy.AMBASSADOR_LIST_FIELDS:
                        record_audit("ambassador_read", name, identity=identity,
                                     params=summarize_args(root_args.get(name) or {}),
                                     row_count=len(keys), ip=ip, ua=ua)

    @staticmethod
    def _row_count(args: dict, value: Any) -> int | None:
        if isinstance(value, dict):
            for key in ("totalRows", "count", "totalCount", "rowCount", "sharedCount",
                        "approvedCount", "successCount"):
                v = value.get(key)
                if isinstance(v, int):
                    return v
            for key in ("rows", "items", "edges", "results"):
                v = value.get(key)
                if isinstance(v, list):
                    return len(v)
        n = _max_list_len(args)
        return n or None


class ClientsGuardrails(GuardrailsExtension):
    schema_label = "clients"


def guardrail_extensions(extension_cls=ClientsGuardrails) -> list:
    lim = policy.limits()
    return [
        MaxTokensLimiter(max_token_count=lim.max_tokens),
        QueryDepthLimiter(max_depth=lim.max_depth),
        MaxAliasesLimiter(max_alias_count=lim.max_aliases),
        extension_cls,
    ]
