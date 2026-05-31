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

from django.conf import settings

OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

# Generous enough for the model to "think" + return 2–3 short paragraphs,
# tight enough that a hung upstream can't wedge the request loop.
DEFAULT_TIMEOUT_SECONDS = 30.0


class AiUnavailable(Exception):
    """Raised when the AI text service is unconfigured or the call fails.

    Carries a short, user-safe message (no key, no stack trace) suitable
    for surfacing straight to the client as a degradation reason.
    """


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
    api_key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        raise AiUnavailable("OpenAI is not configured (set OPENAI_API_KEY).")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _ask(model_name: str) -> str:
        # Use ``max_completion_tokens`` (not the deprecated ``max_tokens``)
        # and omit ``temperature`` so the SAME call works for both standard
        # chat models (gpt-4o-mini) and the newer GPT-5 / reasoning family,
        # which reject ``max_tokens`` and any non-default temperature.
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max_tokens,
        }
        body = _post_json(
            OPENAI_CHAT_COMPLETIONS_URL,
            payload,
            headers,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        return _extract_message_text(body)

    configured = (
        getattr(settings, "OPENAI_MODEL", "") or ""
    ).strip() or "gpt-4o-mini"

    try:
        return _ask(configured)
    except AiUnavailable:
        # Configured model unavailable on this account (e.g. a not-yet-
        # enabled id) or it returned nothing usable — fall back to a
        # known-good default so the feature degrades to a working model
        # instead of breaking entirely.
        if configured == "gpt-4o-mini":
            raise
        return _ask("gpt-4o-mini")


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
