"""Brand chat: a read-only conversation about one brand's own Spark data.

A brand user asks "Which stores should we hit in December?" and the model
answers by calling the tools in :mod:`brand_chat.tools`, every one pinned
to the brand worked out here from the caller's membership.

The rules, in the order a question meets them:

1. **Which brand.** :func:`resolve_tenant`. A brand (client) user gets a
   brand they are an active member of: the one asked for when they belong
   to it, else their own (the dashboard's rule). Ignite admins may pick any
   brand. Anyone else (brand ambassadors included) gets nothing.
2. **Limits.** :func:`check_limits`: questions per person per hour,
   questions per brand per ops day, and an estimated spend cap per brand
   and across all brands per ops day (Pacific). A refused question is
   logged and costs nothing.
3. **The model.** :func:`utils.ai_text.chat_with_tools` with at most
   ``BRAND_CHAT_MAX_TOOL_CALLS`` tool calls and a deadline. Tool results
   are scrubbed of brand ambassador names, emails and phone numbers
   (:class:`spark_mcp.privacy.Scrubber`) before the model sees them, and
   field-typed text is marked as untrusted data.
4. **Grounding.** Every number in the answer must appear in this turn's
   tool results (or in an earlier checked answer that does not cite a
   stale year, or today's date): :func:`recaps.explain_text.unsupported_numbers`,
   the check the dashboard's explain text and chart answers use. One retry
   names the figures that did not check out; if it still fails, the reader
   gets :data:`NOT_GROUNDED`. Relative dates are resolved in
   :mod:`brand_chat.dates` before a tool runs, so a model that asks for
   2022 or 2023 is given the current calendar instead.
5. **Output.** The answer is scrubbed again, and the source chips and store
   links are built here from the tools that actually ran, never from text
   the model wrote.

Every turn is a :class:`brand_chat.models.BrandChatTurn` row: question,
answer, tools used, tokens and estimated cost.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid as uuid_lib
from datetime import datetime, time as dtime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from django.db.models import Sum
from django.utils import timezone

from . import conf
from .dates import (
    align_tool_args,
    grounding_history,
    month_before,
    next_month_bounds,
    previous_month,
    system_prompt,
    user_message,
)

logger = logging.getLogger(__name__)

OPS_TZ = ZoneInfo("America/Los_Angeles")
MAX_QUESTION_CHARS = 500
MAX_ANSWER_CHARS = 4000
MAX_STORE_LINKS = 5

NOT_GROUNDED = "I couldn't confirm that from your data."
FAILED = "Brand chat couldn't answer just now. Try again in a moment."
DISABLED = "Brand chat is turned off right now."
HOURLY_LIMIT = "That's a lot of questions in an hour. Try again a little later."
TENANT_DAILY_LIMIT = "Your team has reached today's limit for brand chat. It resets at midnight Pacific."
SPEND_LIMIT = "Brand chat has reached today's usage limit. It resets at midnight Pacific."

ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "used_tools"],
    "properties": {
        "answer": {"type": "string"},
        "used_tools": {"type": "array", "items": {"type": "string"}},
    },
}

_RETRY = (
    "These figures in your answer are not in the tool results: {bad}. Rewrite the "
    "answer using only figures that appear in the tool results, copied exactly. "
    "Leave out anything you cannot support. Reply as JSON as before."
)

_LIST_MARKER_RE = re.compile(r"^(\s*)\d{1,2}[.)]\s+", re.MULTILINE)
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]{1,200}>")


def strip_links(text: str) -> str:
    """Answers carry no links, images or HTML of the model's making: a
    comment typed in the field could ask for one. Links to Spark pages are
    added separately, from the tools that ran."""
    text = _MD_IMAGE_RE.sub(r"\1", text or "")
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("[link removed]", text)
    return _HTML_RE.sub("", text)


# ─── Who and which brand ──────────────────────────────────────────


def is_admin(user) -> bool:
    from spark_mcp.access import is_admin as _admin

    return bool(user and user.is_active and _admin(user))


def _member_tenants(user):
    from pilot.models import WorkspaceMembership
    from tenants.models import Tenant

    qs = (
        Tenant.objects.filter(tenanted_users__user_id=user.pk, tenanted_users__is_active=True)
        .distinct()
    )
    active_brand_tids = set(
        WorkspaceMembership.objects.filter(
            user_id=user.pk,
            status=WorkspaceMembership.STATUS_ACTIVE,
            workspace_role__in=WorkspaceMembership.BRAND_ROLES,
        ).values_list("tenant_id", flat=True)
    )
    blocked_tids = (
        set(
            WorkspaceMembership.objects.filter(
                user_id=user.pk,
                status=WorkspaceMembership.STATUS_ACTIVE,
                workspace_role__in=WorkspaceMembership.PROVIDER_ROLES,
            ).values_list("tenant_id", flat=True)
        )
        | set(
            WorkspaceMembership.objects.filter(
                user_id=user.pk,
                status=WorkspaceMembership.STATUS_REVOKED,
            ).values_list("tenant_id", flat=True)
        )
    ) - active_brand_tids
    return qs.exclude(id__in=blocked_tids).order_by("id")


def _parse_tenant_ref(raw) -> int | None:
    from tenants.models import Tenant
    from utils.graphql.mixins import resolve_id_to_int

    text = str(raw or "").strip()
    if not text:
        return None
    try:
        uuid_lib.UUID(text)
        return Tenant.objects.filter(uuid=text).values_list("id", flat=True).first()
    except ValueError:
        pass
    try:
        return int(resolve_id_to_int(text))
    except Exception:  # noqa: BLE001
        return None


def resolve_tenant(user, requested):
    """The brand this person's chat reads, or None. See the module notes."""
    from spark_mcp.access import is_brand_user
    from tenants.models import Tenant

    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return None
    wanted = _parse_tenant_ref(requested)
    if is_admin(user):
        return Tenant.objects.filter(id=wanted).first() if wanted else None
    if not is_brand_user(user):
        return None
    mine = _member_tenants(user)
    if wanted is not None:
        own = mine.filter(id=wanted).first()
        if own is not None:
            return own
    return mine.first()


# ─── Limits ────────────────────────────────────────────────────────


def _ops_day_start(now: datetime) -> datetime:
    local = now.astimezone(OPS_TZ)
    return datetime.combine(local.date(), dtime.min, tzinfo=OPS_TZ)


def _counted(qs):
    from .models import BrandChatTurn

    return qs.exclude(status=BrandChatTurn.STATUS_LIMITED)


def usage(user_id: int, tenant_id: int, now: datetime | None = None) -> dict:
    from .models import BrandChatTurn

    now = now or timezone.now()
    day = _ops_day_start(now)
    rows = BrandChatTurn.objects
    hour = _counted(rows.filter(user_id=user_id, created_at__gte=now - timedelta(hours=1))).count()
    tenant_today = _counted(rows.filter(tenant_id=tenant_id, created_at__gte=day))
    return {
        "user_last_hour": hour,
        "tenant_today": tenant_today.count(),
        "tenant_spend_micro": tenant_today.aggregate(s=Sum("cost_micro_usd"))["s"] or 0,
        "all_spend_micro": rows.filter(created_at__gte=day).aggregate(s=Sum("cost_micro_usd"))["s"] or 0,
    }


def check_limits(user_id: int, tenant_id: int, now: datetime | None = None) -> str | None:
    """None when the question may go to the model, else the reason."""
    if not conf.enabled():
        return DISABLED
    used = usage(user_id, tenant_id, now)
    if used["user_last_hour"] >= conf.questions_per_hour_per_user():
        return HOURLY_LIMIT
    if used["tenant_today"] >= conf.questions_per_day_per_tenant():
        return TENANT_DAILY_LIMIT
    if used["all_spend_micro"] >= conf.daily_cost_cap_usd() * 1_000_000:
        return SPEND_LIMIT
    if used["tenant_spend_micro"] >= conf.tenant_daily_cost_cap_usd() * 1_000_000:
        return SPEND_LIMIT
    return None


def cost_micro_usd(prompt_tokens: int, completion_tokens: int) -> int:
    # price per million tokens × tokens = millionths of a dollar.
    return int(
        round(
            prompt_tokens * conf.input_price_per_million_usd()
            + completion_tokens * conf.output_price_per_million_usd()
        )
    )


# ─── Grounding ─────────────────────────────────────────────────────


def unsupported(answer: str, supplied: str) -> list[str]:
    """Figures in ``answer`` that ``supplied`` does not hold. List markers
    ("1. ") are not figures."""
    from recaps.explain_text import unsupported_numbers

    return unsupported_numbers(_LIST_MARKER_RE.sub(r"\1- ", answer or ""), supplied)


def _parse_answer(text: str) -> tuple[str, list[str]]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return (text or "").strip(), []
    if not isinstance(data, dict):
        return (text or "").strip(), []
    answer = data.get("answer")
    used = data.get("used_tools")
    return (
        answer.strip() if isinstance(answer, str) else "",
        [str(u) for u in used] if isinstance(used, list) else [],
    )


# ─── Sources and links ─────────────────────────────────────────────


def _span(run_args: dict, payload: dict) -> str:
    start = payload.get("start_date") or payload.get("from_date") or run_args.get("start_date")
    end = payload.get("end_date") or payload.get("to_date") or run_args.get("end_date")
    if start and end:
        return f"{start} to {end}"
    if payload.get("week_start"):
        return f"week of {payload['week_start']}"
    if payload.get("months"):
        months = payload["months"]
        return f"{months[0]} to {months[-1]}" if months else ""
    return ""


def build_sources(runs: list[dict], used_names: list[str]) -> list[dict]:
    from .tools import CHAT_TOOLS

    ok = [r for r in runs if r["ok"]]
    wanted = {n for n in used_names if n in CHAT_TOOLS}
    chosen = [r for r in ok if r["name"] in wanted] or ok
    out: dict[str, dict] = {}
    for run in chosen:
        tool = CHAT_TOOLS[run["name"]]
        payload = run["payload"] if isinstance(run.get("payload"), dict) else {}
        if (
            tool.name == "propose_demo_wave"
            and payload.get("wave_url")
            and int(payload.get("proposed_count") or 0) > 0
        ):
            n = int(payload["proposed_count"])
            out[tool.name] = {
                "tool": tool.name,
                "label": f"Create {n}-store wave",
                "detail": str(payload.get("wave_summary") or ""),
                "link": str(payload["wave_url"]),
                "link_label": tool.link_label,
            }
            continue
        if tool.name == "search_spark_help":
            matches = payload.get("matches") if isinstance(payload.get("matches"), list) else []
            added_any = False
            for idx, m in enumerate(matches[:3]):
                if not isinstance(m, dict):
                    continue
                route = str(m.get("route_path") or "").strip() or "/setup-guide"
                title = str(m.get("title") or "Spark Help").strip()
                rlabel = str(m.get("route_label") or "Open in Spark").strip()
                out[f"search_spark_help:{idx}:{route}"] = {
                    "tool": f"search_spark_help:{idx}",
                    "label": rlabel,
                    "detail": title,
                    "link": route,
                    "link_label": rlabel,
                }
                added_any = True
            if added_any:
                continue
        span = _span(run["args"], payload)
        held = out.setdefault(
            tool.name,
            {
                "tool": tool.name,
                "label": tool.label,
                "detail": "",
                "link": tool.link,
                "link_label": tool.link_label,
            },
        )
        if span and span not in held["detail"]:
            held["detail"] = f"{held['detail']}; {span}" if held["detail"] else span
    return list(out.values())


_STORE_TOOLS = {
    "get_store_performance",
    "propose_demo_wave",
    "list_recaps",
    "get_consumer_breakdown",
    "get_store_recommendations",
    "list_upcoming_events",
}


def store_links(tenant_id: int, answer: str, runs: list[dict]) -> list[dict]:
    """Store pages for stores the answer names, from the tenant's own
    store grouping (a name that is several stores gets no link)."""
    if not any(r["ok"] and r["name"] in _STORE_TOOLS for r in runs):
        return []
    from recaps.store_places import named_store_links

    try:
        known = named_store_links(tenant_id)
    except Exception:  # noqa: BLE001
        logger.exception("brand chat store links failed tenant=%s", tenant_id)
        return []
    lowered = (answer or "").casefold()
    out = []
    for row in sorted(known, key=lambda r: -len(r["name"])):
        name = row["name"]
        if len(name) < 4 or not row.get("store_key"):
            continue
        if re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", lowered):
            out.append({"label": name, "url": f"/store/{quote(str(row['store_key']), safe='')}"})
        if len(out) >= MAX_STORE_LINKS:
            break
    return out


# ─── Asking ────────────────────────────────────────────────────────


def _history_messages(user_id: int, tenant_id: int, conversation) -> tuple[list[dict], list[str]]:
    """Earlier answered turns of this chat as messages, and each answer
    (already checked) as its own string so a stale year drops the whole turn."""
    from .models import BrandChatTurn

    rows = list(
        BrandChatTurn.objects.filter(
            user_id=user_id,
            tenant_id=tenant_id,
            conversation=conversation,
            cleared_at__isnull=True,
            status=BrandChatTurn.STATUS_ANSWERED,
        ).order_by("-created_at", "-id")[: conf.history_turns()]
    )
    rows.reverse()
    messages: list[dict] = []
    for row in rows:
        messages.append({"role": "user", "content": row.question})
        messages.append({"role": "assistant", "content": row.answer})
    return messages, [r.answer for r in rows]


def _new_conversation_id(raw) -> uuid_lib.UUID:
    try:
        return uuid_lib.UUID(str(raw))
    except (TypeError, ValueError):
        return uuid_lib.uuid4()


def ask(user, tenant, question: str, conversation=None, *, current_path: str | None = None):
    """Answer one question. Always returns a saved :class:`BrandChatTurn`."""
    from spark_mcp.privacy import Scrubber
    from spark_mcp.tools import ToolContext
    from utils.ai_text import AiUnavailable, chat_with_tools

    from . import tools as chat_tools
    from .help_knowledge import (
        RESTRICTED_INTERNAL_REFUSAL,
        _MIN_CONFIDENT_SCORE,
        is_ignite_admin_user,
        is_restricted_internal_topic,
        normalize_route,
        record_help_gap,
        search_help_knowledge,
    )
    from .models import BrandChatTurn

    question = " ".join((question or "").split())[:MAX_QUESTION_CHARS]
    clean_path = normalize_route(current_path)
    caller_is_admin = is_ignite_admin_user(user)
    convo_id = _new_conversation_id(conversation)
    turn = BrandChatTurn(tenant=tenant, user=user, conversation=convo_id, question=question)

    if is_restricted_internal_topic(question):
        turn.status = BrandChatTurn.STATUS_ANSWERED
        turn.answer = RESTRICTED_INTERNAL_REFUSAL
        turn.save()
        return turn

    refused = check_limits(user.pk, tenant.id)
    if refused:
        turn.status, turn.answer = BrandChatTurn.STATUS_LIMITED, refused
        turn.save()
        return turn

    started = time.monotonic()
    scrubber = Scrubber(tenant.id)
    ctx = ToolContext(tenant_id=tenant.id, tenant_name=tenant.name, user_id=user.pk, connection=None)
    runs: list[dict] = []

    history, earlier = _history_messages(user.pk, tenant.id, convo_id)
    today = chat_tools.today_date()
    earlier = grounding_history(earlier, today)

    def run_tool(name: str, args: dict) -> tuple[str, bool]:
        if name == "search_spark_help":
            q_arg = str((args or {}).get("query") or question).strip() or question
            if is_restricted_internal_topic(q_arg) or is_restricted_internal_topic(question):
                payload = {
                    "restricted": True,
                    "matches": [],
                    "note": RESTRICTED_INTERNAL_REFUSAL,
                }
                text = json.dumps(payload, default=str, ensure_ascii=False)
                runs.append(
                    {
                        "name": name,
                        "args": {"query": q_arg},
                        "payload": payload,
                        "text": text,
                        "ok": True,
                        "rows": 0,
                    }
                )
                return text, True
            hits, mode = search_help_knowledge(
                q_arg,
                is_admin=caller_is_admin,
                tenant_id=tenant.id,
                current_path=clean_path,
                top_k=4,
            )
            top_score = hits[0].score if hits else 0.0
            if not hits or top_score < _MIN_CONFIDENT_SCORE:
                record_help_gap(
                    question,
                    is_admin=caller_is_admin,
                    route_path=clean_path,
                    source_surface="ask_spark",
                    reason="no_match" if not hits else "low_confidence",
                    tenant_id=tenant.id,
                    user=user,
                )
            payload = {
                "matched_via": mode,
                "audience_scope": "admin" if caller_is_admin else "client",
                "current_page": clean_path or None,
                "matches": [
                    {
                        "title": h.title,
                        "category": h.category,
                        "route_path": h.route_path,
                        "route_label": h.route_label,
                        "score": h.score,
                        "content": h.body,
                    }
                    for h in hits
                ],
                "note": (
                    "No strong Spark Help article matched this question; tell the reader plainly "
                    "and point them to /help if needed."
                    if (not hits or top_score < _MIN_CONFIDENT_SCORE)
                    else "Answer strictly from these Spark Help documents."
                ),
            }
            text = json.dumps(payload, default=str, ensure_ascii=False)
            runs.append(
                {
                    "name": name,
                    "args": {"query": q_arg},
                    "payload": payload,
                    "text": text,
                    "ok": True,
                    "rows": len(hits),
                }
            )
            return text, True

        aligned = align_tool_args(args, question, today)
        if aligned.refuse:
            payload, rows, ok = {"error": aligned.note}, 0, False
            logged = dict(args or {})
        else:
            payload, rows, ok = chat_tools.run(ctx, name, aligned.args)
            if aligned.note and ok and isinstance(payload, dict):
                payload = {**payload, "date_note": aligned.note}
            logged = dict(aligned.args)
        payload = scrubber.deep(payload)
        text = json.dumps(payload, default=str, ensure_ascii=False)
        runs.append(
            {"name": name, "args": logged, "payload": payload, "text": text, "ok": ok, "rows": rows}
        )
        return text, ok

    messages = [
        {"role": "system", "content": system_prompt(tenant.name, today, is_admin=caller_is_admin)},
        *history,
        {"role": "user", "content": user_message(question, today, current_path=clean_path or None)},
    ]
    tool_defs = chat_tools.openai_tools()
    prompt_tokens = completion_tokens = 0
    model = ""
    answer, used = "", []
    try:
        result = chat_with_tools(
            messages,
            tool_defs,
            run_tool,
            max_tool_calls=conf.max_tool_calls(),
            max_rounds=conf.max_rounds(),
            timeout=conf.request_timeout_seconds(),
            deadline_seconds=conf.turn_deadline_seconds(),
            response_schema=ANSWER_SCHEMA,
        )
        prompt_tokens, completion_tokens, model = (
            result.prompt_tokens,
            result.completion_tokens,
            result.model,
        )
        answer, used = _parse_answer(result.text)
        status = BrandChatTurn.STATUS_ANSWERED if answer else BrandChatTurn.STATUS_FAILED

        supplied = "\n".join(r["text"] for r in runs if r["ok"]) + f"\nTODAY {today.isoformat()}\n" + earlier
        bad = unsupported(answer, supplied) if answer else []
        if answer and bad:
            follow = [
                *result.messages,
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": _RETRY.format(bad=", ".join(bad[:10]))},
            ]
            try:
                again = chat_with_tools(
                    follow,
                    tool_defs,
                    run_tool,
                    max_tool_calls=0,
                    max_rounds=1,
                    timeout=conf.request_timeout_seconds(),
                    deadline_seconds=conf.turn_deadline_seconds(),
                    response_schema=ANSWER_SCHEMA,
                    allow_tools=False,
                )
                prompt_tokens += again.prompt_tokens
                completion_tokens += again.completion_tokens
                answer, used = _parse_answer(again.text)
                bad = unsupported(answer, supplied) if answer else ["(empty)"]
            except AiUnavailable:
                bad = ["(retry failed)"]
            if bad:
                status, answer = BrandChatTurn.STATUS_NOT_GROUNDED, NOT_GROUNDED
    except AiUnavailable as exc:
        logger.info("brand chat unavailable tenant=%s: %s", tenant.id, exc)
        status, answer = BrandChatTurn.STATUS_FAILED, FAILED
    except Exception:  # noqa: BLE001
        logger.exception("brand chat failed tenant=%s", tenant.id)
        status, answer = BrandChatTurn.STATUS_FAILED, FAILED

    if status == BrandChatTurn.STATUS_FAILED and not answer:
        answer = FAILED
    answer = scrubber.text(strip_links(answer))[:MAX_ANSWER_CHARS]
    if status == BrandChatTurn.STATUS_ANSWERED:
        turn.sources = build_sources(runs, used)
        turn.links = store_links(tenant.id, answer, runs)
    turn.answer = answer
    turn.status = status
    turn.tools_used = [
        {"name": r["name"], "args": _log_args(r["args"]), "ok": r["ok"], "rows": r["rows"]} for r in runs
    ]
    turn.tool_calls = len(runs)
    turn.model = model[:80]
    turn.prompt_tokens = prompt_tokens
    turn.completion_tokens = completion_tokens
    turn.cost_micro_usd = cost_micro_usd(prompt_tokens, completion_tokens)
    turn.duration_ms = int((time.monotonic() - started) * 1000)
    turn.save()
    logger.info(
        "brand chat turn tenant=%s user=%s status=%s tools=%s tokens=%s/%s cost_micro=%s",
        tenant.id,
        user.pk,
        status,
        ",".join(r["name"] for r in runs) or "-",
        prompt_tokens,
        completion_tokens,
        turn.cost_micro_usd,
    )
    return turn


def _log_args(args: dict) -> dict:
    from spark_mcp.tools import summarize_args

    return summarize_args(args)


# ─── History ───────────────────────────────────────────────────────


def visible_turns(user_id: int, tenant_id: int, conversation=None) -> tuple[uuid_lib.UUID | None, list]:
    """The person's own chat for this brand: the asked conversation, or the
    latest one. Cleared turns are never shown."""
    from .models import BrandChatTurn

    mine = BrandChatTurn.objects.filter(user_id=user_id, tenant_id=tenant_id, cleared_at__isnull=True)
    convo = None
    if conversation:
        try:
            convo = uuid_lib.UUID(str(conversation))
        except ValueError:
            convo = None
    if convo is None:
        latest = mine.order_by("-created_at", "-id").first()
        if latest is None:
            return None, []
        convo = latest.conversation
    return convo, list(mine.filter(conversation=convo).order_by("created_at", "id")[:50])


def clear(user_id: int, tenant_id: int, conversation=None) -> int:
    """Wipe the text of the person's own turns for this brand (one chat or
    all). The log fields (tools, tokens, cost) stay for limits and spend."""
    from .models import BrandChatTurn

    rows = BrandChatTurn.objects.filter(user_id=user_id, tenant_id=tenant_id, cleared_at__isnull=True)
    if conversation:
        try:
            rows = rows.filter(conversation=uuid_lib.UUID(str(conversation)))
        except ValueError:
            return 0
    return rows.update(question="", answer="", sources=[], links=[], cleared_at=timezone.now())


# ─── Suggested questions ───────────────────────────────────────────


def suggestions(tenant_id: int) -> list[str]:
    """Up to four questions this brand's own data can answer. Worked out
    from cheap counts; no model. Cached ten minutes per brand."""
    from django.core.cache import cache

    key = f"brand_chat:suggestions:v2:{tenant_id}"
    cached = cache.get(key)
    if isinstance(cached, list):
        return cached
    out = _suggestions(tenant_id)
    cache.set(key, out, 600)
    return out


def _suggestions(tenant_id: int) -> list[str]:
    from consumer_mentions.models import RecapMentionTag
    from events.models import Request
    from recaps.models import CustomRecap, Recap
    from spark_mcp.tools import _ops_today

    today = _ops_today()
    last_start, _last_end = previous_month(today)
    before_start, _before_end = month_before(last_start)
    next_start, _next_end = next_month_bounds(today)
    last_label = last_start.strftime("%B %Y")
    before_label = before_start.strftime("%B %Y")
    next_label = next_start.strftime("%B %Y")
    out: list[str] = []
    recent = today - timedelta(days=62)
    has_recaps = (
        CustomRecap.objects.filter(tenant_id=tenant_id, approved=True, created_at__date__gte=recent).exists()
        or Recap.objects.filter(event__tenant_id=tenant_id, approved=True, created_at__date__gte=recent).exists()
    )
    if has_recaps:
        out.append(f"Which stores converted best in {last_label}?")
    if RecapMentionTag.objects.filter(tenant_id=tenant_id).exists():
        theme = _top_theme(tenant_id)
        spoken = f"What are shoppers saying about {theme}" if theme else "What are shoppers saying about price"
        out.append(f"{spoken} in the last 90 days?")
        if len(out) < 4:
            out.append("Which competitor brands are shoppers mentioning in the last six months?")
    if has_recaps or CustomRecap.objects.filter(tenant_id=tenant_id, approved=True).exists():
        out.append(f"Which stores should we hit in {next_label}?")
    if len(out) < 4 and has_recaps:
        out.append("How is our on-shelf rate over the last 30 days?")
    if Request.objects.filter(
        tenant_id=tenant_id, deleted_at__isnull=True, date__date__gte=today, date__date__lte=today + timedelta(days=14)
    ).exists():
        out.append("What's coming up in the next two weeks?")
    if len(out) < 4:
        out.append(f"How did {last_label} compare with {before_label}?")
    if len(out) < 4:
        out.append("What's waiting on my approval?")
    return out[:4]


def _top_theme(tenant_id: int) -> str | None:
    """The most-mentioned theme label in the last six months, lower-cased."""
    try:
        from consumer_mentions.trends import get_mention_trends

        data = get_mention_trends(tenant_id)
    except Exception:  # noqa: BLE001
        return None
    themes = sorted(data.get("themes") or [], key=lambda s: -int(s.get("total") or 0))
    for s in themes:
        label = str(s.get("label") or "").strip()
        if label and len(label) <= 40:
            return label[0].lower() + label[1:]
    return None
