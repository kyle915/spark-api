"""Thin OpenAI Chat Completions client for short generated copy.

A single helper — :func:`generate_summary` — that POSTs a system+user
prompt to the OpenAI Chat Completions REST endpoint and returns the
assistant's text. It is deliberately tiny: no streaming, no SDK.
:func:`chat_with_tools` adds function tools with strict schemas, a cap on
tool calls per turn and a deadline (brand chat, ``brand_chat.service``). The only consumer today is the on-demand campaign-report
executive summary (``recaps.report_types.campaignReportAiSummary``), but
the signature is generic so other "write me a paragraph" features can
reuse it.

Design rules:

* **Env-keyed, never hardcoded.** The API key comes from
  ``settings.OPENAI_API_KEY`` (default ``""``). When it's empty we raise
  :class:`AiUnavailable` *before* making any network call, so the feature
  degrades to a clear "not configured" state instead of a 401.
* **No new dependency.** Uses ``requests`` (already vendored as a
  transitive dependency, present in ``uv.lock``). If it ever stops being
  importable we fall back to the stdlib ``urllib.request`` automatically.
* **Never leak the key or a stack trace.** Every HTTP / parse failure is
  caught and re-raised as :class:`AiUnavailable` with a short,
  human-readable reason. The Authorization header is never echoed back.

The resolver that calls this catches :class:`AiUnavailable` (and any
other ``Exception``) and turns it into ``ok=false`` + ``reason=…`` rather
than letting it bubble out of GraphQL.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Callable, TypeVar

from django.conf import settings

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

# Generous enough for the model to "think" + return 2–3 short paragraphs,
# tight enough that a hung upstream can't wedge the request loop.
DEFAULT_TIMEOUT_SECONDS = 30.0

# Known-good fallback model: a standard chat model available on essentially
# every account, used when the configured model is unavailable OR doesn't
# support a requested feature (e.g. structured outputs).
_FALLBACK_MODEL = "gpt-4o-mini"

# Reasoning models (the GPT-5 and GPT-6 families) spend part of
# ``max_completion_tokens`` thinking before they write. A small cap, sized
# for the visible answer alone, can be used up before any text comes back,
# which reads as an empty completion and drops the call to the fallback
# model. The cap is a ceiling, not a spend: short answers still cost what
# they use.
_MIN_COMPLETION_TOKENS = 4000

_T = TypeVar("_T")


def _completion_cap(max_tokens: int) -> int:
    """The completion ceiling to send: never below the reasoning floor."""
    return max(int(max_tokens), _MIN_COMPLETION_TOKENS)


class AiUnavailable(Exception):
    """Raised when the AI text service is unconfigured or the call fails.

    Carries a short, user-safe message (no key, no stack trace) suitable
    for surfacing straight to the client as a degradation reason.
    """


def _auth_headers() -> dict:
    """Build the OpenAI request headers, raising if the key is unset.

    Centralises the one place the API key is read so the
    :class:`AiUnavailable` "not configured" guard is identical for every
    caller. The Authorization header is built here and never echoed back.
    """
    api_key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        raise AiUnavailable("OpenAI is not configured (set OPENAI_API_KEY).")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _configured_model() -> str:
    """The configured chat model, defaulting to the known-good fallback."""
    return (getattr(settings, "OPENAI_MODEL", "") or "").strip() or _FALLBACK_MODEL


def _small_model() -> str:
    """The model for short sorting jobs (is this a store name, which bucket
    is this photo, does this number look wrong). These run many times and
    need no long reasoning, so they use a smaller, cheaper model than the
    one that writes summaries and answers questions."""
    return (
        getattr(settings, "OPENAI_MODEL_SMALL", "") or ""
    ).strip() or _configured_model()


def _with_model_fallback(ask: Callable[[str], _T], *, small: bool = False) -> _T:
    """Run ``ask(model)`` against the configured model, then the fallback.

    ``ask`` takes a model id and returns its result (or raises
    :class:`AiUnavailable`). If the configured model is unavailable on this
    account — a not-yet-enabled id, or one that returned nothing usable —
    we retry once against :data:`_FALLBACK_MODEL` so the feature degrades to
    a working model instead of breaking entirely. When the configured model
    *is* the fallback, the original error propagates unchanged.

    ``small`` picks :func:`_small_model` instead of the main model.
    """
    configured = _small_model() if small else _configured_model()
    try:
        return ask(configured)
    except AiUnavailable:
        if configured == _FALLBACK_MODEL:
            raise
        return ask(_FALLBACK_MODEL)


def _chat_messages(system: str, user: str) -> list[dict]:
    """The standard two-message system+user chat payload."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def generate_summary(
    system: str, user: str, *, max_tokens: int = 500, small: bool = False
) -> str:
    """Generate a short piece of text from a system + user prompt.

    Args:
        system: The system prompt (role/persona + constraints).
        user: The user prompt (the actual content to summarize).
        max_tokens: Cap on the completion length.

    Returns:
        The assistant message text (stripped).

    Raises:
        AiUnavailable: If ``OPENAI_API_KEY`` is unset, or the HTTP call /
            response parsing fails for any reason. The message is always
            safe to show a user — it never contains the API key.
    """
    headers = _auth_headers()

    def _ask(model_name: str) -> str:
        # Use ``max_completion_tokens`` (not the deprecated ``max_tokens``)
        # and omit ``temperature`` so the SAME call works for both standard
        # chat models (gpt-4o-mini) and the newer GPT-5 / reasoning family,
        # which reject ``max_tokens`` and any non-default temperature.
        payload = {
            "model": model_name,
            "messages": _chat_messages(system, user),
            "max_completion_tokens": _completion_cap(max_tokens),
        }
        body = _post_json(
            OPENAI_CHAT_COMPLETIONS_URL,
            payload,
            headers,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        return _extract_message_text(body)

    return _with_model_fallback(_ask, small=small)


# JSON Schema for the structured Q&A response (OpenAI "structured outputs").
# The model MUST return an object with a text ``answer`` plus an optional
# ``chart`` (null when no visualization is warranted). ``strict`` mode
# requires every object to set ``additionalProperties: false`` and list ALL
# of its properties in ``required`` — nullable fields express "optional" via
# a union type (e.g. ``["object", "null"]``), NOT by omission.
_AI_ANSWER_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "chart"],
    "properties": {
        "answer": {"type": "string"},
        "chart": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["type", "title", "labels", "series"],
            "properties": {
                "type": {"type": "string", "enum": ["bar", "line"]},
                "title": {"type": ["string", "null"]},
                "labels": {"type": "array", "items": {"type": "string"}},
                "series": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["label", "data"],
                        "properties": {
                            "label": {"type": "string"},
                            "data": {
                                "type": "array",
                                "items": {"type": "number"},
                            },
                        },
                    },
                },
            },
        },
    },
}


def generate_structured_answer(
    system: str, user: str, *, max_tokens: int = 8000
) -> tuple[str, dict | None]:
    """Generate a text answer plus an OPTIONAL structured chart spec.

    Asks OpenAI for a JSON object — ``{"answer": str, "chart": object|null}``
    — using **structured outputs** (``response_format`` json_schema, strict),
    so a question best answered by a visualization can come back with a chart
    the frontend renders, while everything else just gets text.

    Args:
        system: The system prompt (must instruct the model on when/how to
            emit a ``chart``; see the answer system prompts in
            ``recaps.report_types``).
        user: The user prompt (the data + the question).
        max_tokens: Cap on the completion length.

    Returns:
        A ``(answer, chart)`` tuple. ``answer`` is the stripped text answer;
        ``chart`` is the parsed chart dict, or ``None`` when the model
        omitted it (or anything about the structured path failed).

    Raises:
        AiUnavailable: ONLY when the service is unconfigured (no
            ``OPENAI_API_KEY``). Every OTHER failure — an HTTP/model error, a
            model that doesn't support ``json_schema``, or unparseable JSON —
            is swallowed and the call FALLS BACK to plain
            :func:`generate_summary`, returning ``(text, None)``. The text
            answer must always work; the chart is a bonus.
    """
    # Read the key up front so a genuinely-unconfigured service raises
    # AiUnavailable *before* we attempt (and then swallow) the structured
    # call — otherwise the fallback would just re-raise the same error.
    headers = _auth_headers()

    def _ask(model_name: str) -> tuple[str, dict | None]:
        payload = {
            "model": model_name,
            "messages": _chat_messages(system, user),
            "max_completion_tokens": _completion_cap(max_tokens),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ai_answer",
                    "strict": True,
                    "schema": _AI_ANSWER_JSON_SCHEMA,
                },
            },
        }
        body = _post_json(
            OPENAI_CHAT_COMPLETIONS_URL,
            payload,
            headers,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        content = _extract_message_text(body)
        parsed = json.loads(content)  # JSONDecodeError handled below
        if not isinstance(parsed, dict):
            raise ValueError("structured answer was not a JSON object")
        answer = parsed.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("structured answer missing text")
        chart = parsed.get("chart")
        return answer.strip(), (chart if isinstance(chart, dict) else None)

    try:
        return _with_model_fallback(_ask)
    except Exception:
        # ANY structured-path failure (HTTP/model error incl. a model that
        # rejects json_schema, empty completion, bad/garbled JSON) degrades
        # to the plain text answer. We deliberately catch broadly — the text
        # answer MUST always work. AiUnavailable from the *unconfigured*
        # guard already raised above, before we got here, so it still
        # propagates; only mid-flight failures land in this fallback.
        return generate_summary(system, user, max_tokens=max_tokens), None


def generate_json(
    system: str,
    user: str,
    *,
    schema: dict,
    max_tokens: int = 2000,
    small: bool = False,
    timeout: float | None = None,
) -> dict | None:
    """Generate a strict-JSON object matching ``schema`` (structured outputs).

    The general-purpose sibling of :func:`generate_structured_answer`: where
    that one is hard-wired to the ``{answer, chart}`` shape, this takes an
    arbitrary caller-supplied JSON Schema and returns the parsed object. Uses
    OpenAI **structured outputs** (``response_format`` json_schema, strict),
    reusing the same shared auth + model-fallback path every other helper here
    uses, so a not-yet-enabled configured model retries against
    :data:`_FALLBACK_MODEL`.

    Args:
        system: The system prompt (role/persona + constraints).
        user: The user prompt (the data the model reasons over).
        schema: A JSON Schema for the response object. For strict mode it must
            set ``additionalProperties: false`` and list ALL properties in
            ``required`` at every object level (nullable fields express
            "optional" via a union type, e.g. ``["string", "null"]``).
        max_tokens: Cap on the completion length.
        timeout: Seconds to wait on each HTTP attempt. Defaults to
            :data:`DEFAULT_TIMEOUT_SECONDS`. A caller someone is waiting on
            (the recap check on the check-in form) passes a short one. The
            model fallback can make a second attempt, so a caller with a hard
            limit must also bound the whole call itself.

    Returns:
        The parsed JSON object (a ``dict``) on success, or **None** on ANY
        mid-flight failure — an HTTP/model error (including a model that
        rejects ``json_schema``), an empty completion, or unparseable / non-
        object JSON. Callers degrade gracefully on ``None``.

    Raises:
        AiUnavailable: ONLY when the service is unconfigured (no
            ``OPENAI_API_KEY``). Every other failure returns ``None`` instead
            of raising, so a caller can fall back to an empty result without a
            try/except around the parse path.
    """
    # Read the key up front so a genuinely-unconfigured service raises
    # AiUnavailable here (the documented "no key" contract) rather than being
    # swallowed into the None return below.
    headers = _auth_headers()

    def _ask(model_name: str) -> dict:
        payload = {
            "model": model_name,
            "messages": _chat_messages(system, user),
            "max_completion_tokens": _completion_cap(max_tokens),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_json",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        body = _post_json(
            OPENAI_CHAT_COMPLETIONS_URL,
            payload,
            headers,
            timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
        )
        content = _extract_message_text(body)
        parsed = json.loads(content)  # JSONDecodeError caught below
        if not isinstance(parsed, dict):
            raise ValueError("structured JSON was not an object")
        return parsed

    try:
        return _with_model_fallback(_ask, small=small)
    except Exception:
        # ANY mid-flight failure (HTTP/model error incl. a model that rejects
        # json_schema, empty completion, bad/garbled JSON, non-object result)
        # degrades to None. The unconfigured-key guard already raised
        # AiUnavailable above, before we got here, so it still propagates.
        return None


def generate_vision_json(
    system: str,
    user: str,
    *,
    image_bytes: bytes,
    content_type: str,
    schema: dict,
    max_tokens: int = 400,
    small: bool = False,
    timeout: float | None = None,
    deadline: float | None = None,
    meta: dict | None = None,
) -> dict | None:
    """Classify one image with a strict JSON schema.

    Same auth and model-fallback path as :func:`generate_json`. The image is
    sent as a data URL on the user message. Callers must not ask the model
    to identify people — this helper does not add a face feature of its own.
    ``timeout`` is seconds per HTTP attempt (default
    :data:`DEFAULT_TIMEOUT_SECONDS`), for a caller someone is waiting on.
    ``deadline``, when given as a ``time.monotonic()`` timestamp, bounds all
    model attempts combined so fallback retries stop once the budget expires.
    ``meta``, when given, is filled with the model that answered and its
    token usage (``model``, ``input_tokens``, ``output_tokens``).

    Raises:
        AiUnavailable: Only when ``OPENAI_API_KEY`` is unset.

    Returns:
        The parsed object, or ``None`` on any mid-flight failure.
    """
    headers = _auth_headers()
    mime = content_type if content_type.startswith("image/") else "image/jpeg"
    data_url = "data:" + mime + ";base64," + base64.b64encode(image_bytes).decode("ascii")

    def _ask(model_name: str) -> dict:
        attempt_timeout = timeout or DEFAULT_TIMEOUT_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                raise AiUnavailable("Vision deadline expired.")
            attempt_timeout = min(attempt_timeout, remaining)
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            "max_completion_tokens": _completion_cap(max_tokens),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "vision_json",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        body = _post_json(
            OPENAI_CHAT_COMPLETIONS_URL,
            payload,
            headers,
            timeout=attempt_timeout,
        )
        if meta is not None:
            usage = body.get("usage") if isinstance(body, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            meta["model"] = str(body.get("model") or model_name)[:64]
            meta["input_tokens"] = usage.get("prompt_tokens")
            meta["output_tokens"] = usage.get("completion_tokens")
        content = _extract_message_text(body)
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("vision JSON was not an object")
        return parsed

    try:
        return _with_model_fallback(_ask, small=small)
    except Exception:
        return None


OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
# Speech-to-text. The configured model first, then the long-standing one.
_TRANSCRIBE_FALLBACK_MODEL = "whisper-1"
# OpenAI refuses files over 25 MB; callers cap well under that.
_TRANSCRIBE_MAX_BYTES = 25 * 1024 * 1024
_TRANSCRIBE_EXTENSIONS = {
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "video/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}


def transcription_extension(content_type: str) -> str | None:
    """The file extension OpenAI expects for this audio type, or None when
    the type is not one it reads. Codec parameters are ignored."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return _TRANSCRIBE_EXTENSIONS.get(base)


def _transcribe_model() -> str:
    return (
        getattr(settings, "OPENAI_TRANSCRIBE_MODEL", "") or ""
    ).strip() or _TRANSCRIBE_FALLBACK_MODEL


def transcribe_audio(
    audio: bytes,
    *,
    content_type: str,
    prompt: str = "",
    language: str = "en",
    timeout: float | None = None,
) -> str:
    """Speech to text for one short recording. The audio is sent once and
    not kept: nothing here writes it anywhere.

    ``prompt`` is a short list of words to spell correctly (product names).

    Raises:
        AiUnavailable: When the key is unset, the audio type is not one
            OpenAI reads, the file is empty or too big, or the call fails.
            The message is safe to show.
    """
    headers = _auth_headers()
    headers.pop("Content-Type", None)  # multipart sets its own boundary
    ext = transcription_extension(content_type)
    if ext is None:
        raise AiUnavailable("That recording format can't be read.")
    if not audio or len(audio) > _TRANSCRIBE_MAX_BYTES:
        raise AiUnavailable("That recording is empty or too long.")
    base_type = content_type.split(";", 1)[0].strip().lower()

    def _ask(model_name: str) -> str:
        fields = {"model": model_name, "response_format": "json"}
        if language:
            fields["language"] = language
        if prompt:
            fields["prompt"] = prompt[:800]
        body = _post_multipart(
            OPENAI_TRANSCRIPTIONS_URL,
            fields,
            ("file", f"recording.{ext}", audio, base_type),
            headers,
            timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
        )
        if not isinstance(body, dict):
            raise AiUnavailable("Speech service returned an unexpected response.")
        err = body.get("error")
        if err:
            raise AiUnavailable("Speech service error.")
        text = body.get("text")
        if not isinstance(text, str):
            raise AiUnavailable("Speech service returned no text.")
        return text.strip()

    configured = _transcribe_model()
    try:
        return _ask(configured)
    except AiUnavailable:
        if configured == _TRANSCRIBE_FALLBACK_MODEL:
            raise
        return _ask(_TRANSCRIBE_FALLBACK_MODEL)


def _post_multipart(
    url: str,
    fields: dict,
    file: tuple[str, str, bytes, str],
    headers: dict,
    *,
    timeout: float,
) -> dict:
    """POST form fields plus one file as multipart; return the decoded JSON.

    ``file`` is ``(form_name, filename, bytes, content_type)``. Errors are
    re-raised as :class:`AiUnavailable` with a short reason, like
    :func:`_post_json`.
    """
    import requests  # noqa: PLC0415 — present in uv.lock

    name, filename, data, ctype = file
    try:
        response = requests.post(
            url,
            data=fields,
            files={name: (filename, data, ctype)},
            headers=headers,
            timeout=timeout,
        )
    except requests.exceptions.Timeout as exc:
        raise AiUnavailable("Speech service timed out.") from exc
    except requests.exceptions.RequestException as exc:
        raise AiUnavailable("Speech service request failed.") from exc
    if response.status_code >= 400:
        raise AiUnavailable(_http_error_reason(response.status_code, response.text))
    try:
        return response.json()
    except ValueError as exc:
        raise AiUnavailable("Speech service returned invalid JSON.") from exc
# ─── Tool calling (function tools) ───────────────────────────────────


class ToolRun:
    """One tool call the model asked for and what came back."""

    __slots__ = ("name", "args", "output", "ok")

    def __init__(self, name: str, args: dict, output: str, ok: bool):
        self.name = name
        self.args = args
        self.output = output
        self.ok = ok


class ChatResult:
    """The model's last message plus everything that led to it.

    ``text`` is the final assistant content (empty when the model gave
    none). ``stopped`` says why the loop ended: ``answered``,
    ``tool_cap`` (the per-turn tool-call cap was hit and the model was made
    to answer without more tools) or ``deadline``.
    """

    def __init__(self):
        self.text = ""
        self.runs: list[ToolRun] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.model = ""
        self.stopped = "answered"
        self.requests = 0
        # The whole conversation as sent, tool results included, so a
        # caller can ask a follow-up (e.g. "use only the figures given").
        self.messages: list[dict] = []


def strict_function_tool(name: str, description: str, parameters: dict) -> dict:
    """A Chat Completions function tool with a strict schema.

    Strict mode needs every property listed in ``required`` and
    ``additionalProperties: false``; an optional argument is a nullable
    union instead. Bounds (``minimum`` …) are dropped: the tool checks its
    own arguments and says what was wrong.
    """
    props: dict = {}
    for key, spec in (parameters.get("properties") or {}).items():
        kind = spec.get("type", "string")
        out = {"type": [kind, "null"] if isinstance(kind, str) else [*kind, "null"]}
        if spec.get("description"):
            out["description"] = spec["description"]
        if spec.get("enum"):
            out["enum"] = [*spec["enum"], None]
        props[key] = out
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(props),
                "additionalProperties": False,
            },
        },
    }


def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    run_tool: Callable[[str, dict], tuple[str, bool]],
    *,
    max_tool_calls: int = 6,
    max_rounds: int = 4,
    max_tokens: int = 1500,
    timeout: float | None = None,
    deadline_seconds: float = 60.0,
    response_schema: dict | None = None,
    allow_tools: bool = True,
) -> ChatResult:
    """Run a Chat Completions conversation with function tools.

    The model may call tools for up to ``max_rounds`` rounds and
    ``max_tool_calls`` calls in all. ``run_tool(name, args)`` runs one and
    returns ``(text for the model, ok)``; it must never raise. Calls past
    the cap are answered with a "limit reached" note and the model is then
    asked once more with ``tool_choice: "none"`` so it answers from what it
    has. Past ``deadline_seconds`` the same last call is made.

    ``response_schema`` (strict JSON schema) shapes the final message.
    ``allow_tools=False`` sends the tools but forbids calling them (a
    follow-up that must answer from results already in ``messages``).

    Raises :class:`AiUnavailable` when the service is unconfigured or the
    first request fails on both the configured and the fallback model.
    Later failures end the loop with whatever text there is (possibly
    none); the caller decides what to show.
    """
    import time as _time

    headers = _auth_headers()
    result = ChatResult()
    started = _time.monotonic()
    convo = list(messages)
    result.messages = convo
    per_request = timeout or DEFAULT_TIMEOUT_SECONDS

    def _payload(model: str, *, allow_tools: bool) -> dict:
        payload = {
            "model": model,
            "messages": convo,
            "max_completion_tokens": _completion_cap(max_tokens),
            "tools": tools,
            "tool_choice": "auto" if allow_tools else "none",
        }
        if allow_tools:
            payload["parallel_tool_calls"] = True
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "chat_answer", "strict": True, "schema": response_schema},
            }
        return payload

    def _send(model: str, *, allow_tools: bool) -> dict:
        body = _post_json(
            OPENAI_CHAT_COMPLETIONS_URL,
            _payload(model, allow_tools=allow_tools),
            headers,
            timeout=per_request,
        )
        if not isinstance(body, dict):
            raise AiUnavailable("AI service returned an unexpected response.")
        err = body.get("error")
        if err:
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise AiUnavailable(f"AI service error: {message or 'unknown error'}")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AiUnavailable("AI service returned no completion.")
        result.requests += 1
        usage = body.get("usage") or {}
        result.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        result.completion_tokens += int(usage.get("completion_tokens") or 0)
        return choices[0].get("message") or {}

    # First request picks the model: the configured one, else the fallback.
    model = _configured_model()
    try:
        message = _send(model, allow_tools=allow_tools)
    except AiUnavailable:
        if model == _FALLBACK_MODEL:
            raise
        model = _FALLBACK_MODEL
        message = _send(model, allow_tools=allow_tools)
    result.model = model

    used = 0
    rounds = 0
    while True:
        calls = message.get("tool_calls") or []
        if not calls:
            content = message.get("content")
            result.text = content.strip() if isinstance(content, str) else ""
            return result
        rounds += 1
        convo.append(
            {
                "role": "assistant",
                "content": message.get("content") or None,
                "tool_calls": calls,
            }
        )
        capped = False
        for call in calls:
            fn = (call or {}).get("function") or {}
            name = str(fn.get("name") or "")
            call_id = (call or {}).get("id") or ""
            if used >= max_tool_calls:
                capped = True
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(
                            {"error": "Tool call limit for this question reached. Answer from the results you have."}
                        ),
                    }
                )
                continue
            used += 1
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}
            output, ok = run_tool(name, args)
            result.runs.append(ToolRun(name, args, output, ok))
            convo.append({"role": "tool", "tool_call_id": call_id, "content": output})

        out_of_time = (_time.monotonic() - started) >= deadline_seconds
        last_round = capped or used >= max_tool_calls or rounds >= max_rounds or out_of_time
        if last_round:
            result.stopped = "deadline" if out_of_time else "tool_cap"
        try:
            message = _send(model, allow_tools=not last_round)
        except AiUnavailable:
            result.text = ""
            result.stopped = "error"
            return result
        if last_round:
            content = message.get("content")
            result.text = content.strip() if isinstance(content, str) else ""
            return result


def _extract_message_text(body: dict) -> str:
    """Pull ``choices[0].message.content`` out of an OpenAI response.

    Raises :class:`AiUnavailable` if the shape isn't what we expect (e.g.
    OpenAI returned an ``error`` object, or an empty completion).
    """
    if not isinstance(body, dict):
        raise AiUnavailable("AI service returned an unexpected response.")

    # OpenAI surfaces problems as a top-level ``error`` object. Lift its
    # message (which is safe — it describes the request, not the key).
    err = body.get("error")
    if err:
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise AiUnavailable(f"AI service error: {message or 'unknown error'}")

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiUnavailable("AI service returned no completion.")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise AiUnavailable("AI service returned an empty completion.")

    return content.strip()


def _post_json(url: str, payload: dict, headers: dict, *, timeout: float) -> dict:
    """POST ``payload`` as JSON and return the decoded JSON response.

    Prefers ``requests`` (already an installed transitive dependency);
    falls back to the stdlib ``urllib.request`` if it isn't importable so
    we never add a hard dependency. Any transport / decode error is
    re-raised as :class:`AiUnavailable` with a short reason — the raw
    exception (which could embed request details) is never surfaced.
    """
    try:
        import requests  # noqa: PLC0415 — optional, prefer-if-present
    except ImportError:
        return _post_json_urllib(url, payload, headers, timeout=timeout)

    try:
        response = requests.post(
            url, json=payload, headers=headers, timeout=timeout
        )
    except requests.exceptions.Timeout as exc:
        raise AiUnavailable("AI service timed out.") from exc
    except requests.exceptions.RequestException as exc:
        raise AiUnavailable("AI service request failed.") from exc

    if response.status_code >= 400:
        raise AiUnavailable(
            _http_error_reason(response.status_code, response.text)
        )

    try:
        return response.json()
    except ValueError as exc:
        raise AiUnavailable("AI service returned invalid JSON.") from exc


def _post_json_urllib(
    url: str, payload: dict, headers: dict, *, timeout: float
) -> dict:
    """Stdlib fallback for :func:`_post_json` (no ``requests`` available)."""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = ""
        raise AiUnavailable(_http_error_reason(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        # Covers timeouts (reason is a socket.timeout) and DNS/connection
        # failures. Don't echo the reason verbatim — keep it generic.
        raise AiUnavailable("AI service request failed.") from exc

    try:
        return json.loads(raw)
    except ValueError as exc:
        raise AiUnavailable("AI service returned invalid JSON.") from exc


def _http_error_reason(status_code: int, raw_body: str) -> str:
    """Build a short, key-safe reason string from an HTTP error response.

    Tries to lift OpenAI's ``error.message`` (which describes the request,
    not the credential) out of the JSON body; falls back to the bare
    status code. Never includes request headers, so the key can't leak.
    """
    message = ""
    try:
        body = json.loads(raw_body) if raw_body else {}
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            message = err.get("message") or ""
        elif isinstance(err, str):
            message = err
    except ValueError:
        message = ""
    if message:
        return f"AI service error (HTTP {status_code}): {message}"
    return f"AI service error (HTTP {status_code})."


def _embedding_model() -> str:
    return (
        getattr(settings, "OPENAI_EMBEDDING_MODEL", "") or ""
    ).strip() or DEFAULT_EMBEDDING_MODEL


def embed_texts(
    texts: list[str],
    *,
    model: str | None = None,
    timeout: float = 15.0,
) -> tuple[list[list[float]], str]:
    """Generate OpenAI semantic embeddings for ``texts`` (``text-embedding-3-small``).

    Returns ``(vectors, model_name)`` in the exact input order. Raises
    :class:`AiUnavailable` when ``OPENAI_API_KEY`` is unset or the request fails.
    """
    cleaned = [" ".join((t or "").split())[:6000] for t in texts]
    if not cleaned:
        return [], (model or _embedding_model())
    headers = _auth_headers()
    chosen_model = (model or _embedding_model()).strip() or DEFAULT_EMBEDDING_MODEL
    payload = {
        "model": chosen_model,
        "input": cleaned,
    }
    body = _post_json(
        OPENAI_EMBEDDINGS_URL,
        payload,
        headers,
        timeout=timeout,
    )
    if not isinstance(body, dict):
        raise AiUnavailable("AI embeddings service returned an unexpected response.")
    if body.get("error"):
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise AiUnavailable(f"AI embeddings error: {msg or 'unknown error'}")
    rows = body.get("data")
    if not isinstance(rows, list) or len(rows) != len(cleaned):
        raise AiUnavailable("AI embeddings response size mismatch.")
    ordered = sorted(rows, key=lambda r: int(r.get("index", 0)))
    vectors: list[list[float]] = []
    for row in ordered:
        emb = row.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise AiUnavailable("AI embeddings response missing vector.")
        vectors.append([float(v) for v in emb])
    return vectors, str(body.get("model") or chosen_model)


def embed_text(
    text: str,
    *,
    model: str | None = None,
    timeout: float = 12.0,
) -> tuple[list[float] | None, str]:
    """Best-effort single-text embedding; returns ``(None, model)`` on any error."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return None, (model or _embedding_model())
    try:
        vectors, used_model = embed_texts([cleaned], model=model, timeout=timeout)
        return (vectors[0] if vectors else None), used_model
    except AiUnavailable:
        return None, (model or _embedding_model())


def cosine_similarity(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity in ``[-1.0, 1.0]`` for two equal-length float vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    import math

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        norm_a += fx * fx
        norm_b += fy * fy
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
