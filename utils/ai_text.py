"""Thin OpenAI Chat Completions client for short generated copy.

A single helper — :func:`generate_summary` — that POSTs a system+user
prompt to the OpenAI Chat Completions REST endpoint and returns the
assistant's text. It is deliberately tiny: no streaming, no function
calling, no SDK. The only consumer today is the on-demand campaign-report
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

import json
from typing import Callable, TypeVar

from django.conf import settings

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

# Generous enough for the model to "think" + return 2–3 short paragraphs,
# tight enough that a hung upstream can't wedge the request loop.
DEFAULT_TIMEOUT_SECONDS = 30.0

# Known-good fallback model: a standard chat model available on essentially
# every account, used when the configured model is unavailable OR doesn't
# support a requested feature (e.g. structured outputs).
_FALLBACK_MODEL = "gpt-4o-mini"

_T = TypeVar("_T")


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


def _with_model_fallback(ask: Callable[[str], _T]) -> _T:
    """Run ``ask(model)`` against the configured model, then the fallback.

    ``ask`` takes a model id and returns its result (or raises
    :class:`AiUnavailable`). If the configured model is unavailable on this
    account — a not-yet-enabled id, or one that returned nothing usable —
    we retry once against :data:`_FALLBACK_MODEL` so the feature degrades to
    a working model instead of breaking entirely. When the configured model
    *is* the fallback, the original error propagates unchanged.
    """
    configured = _configured_model()
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


def generate_summary(system: str, user: str, *, max_tokens: int = 500) -> str:
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
            "max_completion_tokens": max_tokens,
        }
        body = _post_json(
            OPENAI_CHAT_COMPLETIONS_URL,
            payload,
            headers,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        return _extract_message_text(body)

    return _with_model_fallback(_ask)


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
            "max_completion_tokens": max_tokens,
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
