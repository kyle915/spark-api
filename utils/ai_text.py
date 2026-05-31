"""Thin Gemini client for short generated copy.

A single helper — :func:`generate_summary` — that sends a system + user
prompt to Google's Gemini (Generative Language) API and returns the
model's text. It is deliberately tiny: no streaming, no tools, no
multi-turn. The only consumer today is the on-demand campaign-report
executive summary (``recaps.report_types.campaignReportAiSummary``), but
the signature is generic so other "write me a paragraph" features can
reuse it.

Design rules:

* **Env-keyed, never hardcoded.** The API key comes from
  ``settings.GEMINI_API_KEY`` (default ``""``) — the same key the tenant
  insights feature already uses. When it's empty we raise
  :class:`AiUnavailable` *before* any network call, so the feature
  degrades to a clear "not configured" state instead of an auth error.
* **Reuses the installed SDK.** Uses ``google.generativeai`` (already a
  dependency, used by ``tenants.insights.service``), imported lazily so a
  missing library degrades gracefully instead of breaking module import.
* **Resilient to model churn.** When ``GEMINI_MODEL`` isn't pinned we
  discover a current model that supports ``generateContent`` (preferring
  a fast "flash" variant), the same guard the insights service uses.
* **Never leak the key or a stack trace.** Every failure is caught and
  re-raised as :class:`AiUnavailable` with a short, user-safe reason.

The resolver that calls this catches :class:`AiUnavailable` (and any
other ``Exception``) and turns it into ``ok=false`` + ``reason=…`` rather
than letting it bubble out of GraphQL.
"""

from __future__ import annotations

from django.conf import settings

# Used only if discovery is skipped (``GEMINI_MODEL`` pinned) or the
# discovery call fails — a broadly available, fast, cheap default.
DEFAULT_GEMINI_MODEL = "gemini-1.5-flash"

# Generous enough for 2–3 short paragraphs, tight enough that a hung
# upstream can't wedge the request loop.
DEFAULT_TIMEOUT_SECONDS = 30.0


class AiUnavailable(Exception):
    """Raised when the AI text service is unconfigured or the call fails.

    Carries a short, user-safe message (no key, no stack trace) suitable
    for surfacing straight to the client as a degradation reason.
    """


def generate_summary(system: str, user: str, *, max_tokens: int = 500) -> str:
    """Generate a short piece of text from a system + user prompt.

    Args:
        system: The system prompt (role/persona + constraints). Sent as
            Gemini's ``system_instruction``.
        user: The user prompt (the actual content to summarize).
        max_tokens: Cap on the generated length (Gemini
            ``max_output_tokens``).

    Returns:
        The model's text (stripped).

    Raises:
        AiUnavailable: If ``GEMINI_API_KEY`` is unset, the SDK isn't
            importable, or the call / response fails for any reason. The
            message is always safe to show a user — it never contains the
            API key.
    """
    api_key = (getattr(settings, "GEMINI_API_KEY", "") or "").strip()
    if not api_key:
        raise AiUnavailable("AI is not configured (set GEMINI_API_KEY).")

    try:
        import google.generativeai as genai  # noqa: PLC0415 — lazy import
    except ImportError as exc:
        raise AiUnavailable("AI client library is not available.") from exc

    try:
        genai.configure(api_key=api_key)
        model_name = _resolve_model_name(genai)
        model = genai.GenerativeModel(
            model_name, system_instruction=system or None
        )
        response = model.generate_content(
            user,
            generation_config={
                "temperature": 0.4,
                "max_output_tokens": max_tokens,
            },
            request_options={"timeout": DEFAULT_TIMEOUT_SECONDS},
        )
    except AiUnavailable:
        raise
    except Exception as exc:
        # Never surface the raw exception (could embed request details);
        # keep it short and key-safe.
        raise AiUnavailable(f"AI service error: {_safe_reason(exc)}") from exc

    return _extract_text(response)


def _resolve_model_name(genai) -> str:
    """Pick the Gemini model to call.

    A pinned ``settings.GEMINI_MODEL`` wins outright. Otherwise discover a
    model that supports ``generateContent`` (preferring a fast "flash"
    variant), guarding against model-name churn the same way
    ``tenants.insights.service`` does. Falls back to
    :data:`DEFAULT_GEMINI_MODEL` when discovery is unavailable.
    """
    pinned = (getattr(settings, "GEMINI_MODEL", "") or "").strip()
    if pinned:
        return pinned

    try:
        usable = [
            m
            for m in genai.list_models()
            if "generateContent"
            in getattr(m, "supported_generation_methods", [])
        ]
    except Exception:
        return DEFAULT_GEMINI_MODEL

    for m in usable:  # prefer a fast/cheap flash model
        if "flash" in m.name.lower():
            return m.name
    for m in usable:  # else any gemini model
        if "gemini" in m.name.lower():
            return m.name
    return usable[0].name if usable else DEFAULT_GEMINI_MODEL


def _extract_text(response) -> str:
    """Pull the generated text out of a Gemini response.

    Raises :class:`AiUnavailable` if the response carried no usable text
    (e.g. it was blocked by a safety filter or came back empty).
    """
    # ``response.text`` is the happy-path accessor but raises when a
    # candidate has no text parts (safety block / empty). Guard it, then
    # fall back to walking candidates before giving up.
    text = None
    try:
        text = response.text
    except Exception:
        text = None

    if not (text and text.strip()):
        collected = []
        for cand in getattr(response, "candidates", None) or []:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                piece = getattr(part, "text", None)
                if piece:
                    collected.append(piece)
        text = "".join(collected)

    if not (text and text.strip()):
        raise AiUnavailable("AI service returned an empty completion.")
    return text.strip()


def _safe_reason(exc: Exception) -> str:
    """Short, key-safe one-liner describing an upstream failure."""
    reason = (str(exc) or "").strip()
    if not reason:
        return "request failed"
    return reason.splitlines()[0][:200]
