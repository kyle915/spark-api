"""Thin Gemini (google.generativeai) client for freeform-prompt ->
narrative-text generation.

Distinct from utils/ai_text.py, which — despite some stale comments
elsewhere in the codebase calling it "Gemini" — is actually an OpenAI Chat
Completions client. The one REAL Gemini integration that predates this
module is tenants/insights/service.py::InsightsService, which is tightly
coupled to its own ConsumerFeedback/Insights domain. This extracts just
its proven model-discovery + fallback logic as a reusable, domain-agnostic
function rather than routing new callers through that class.

Best-effort by design, matching utils/ai_text.py's degrade contract:
returns None on ANY failure (missing key, no available model, malformed
response) rather than raising, so a caller can always fall back to a
deterministic view instead of the AI-generated one.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _pick_model(genai) -> str:
    """Mirrors InsightsService._call_gemini_api's model-discovery dance:
    prefer a listed model with "gemini" in the name that supports
    generateContent, else the first that does, else a hardcoded fallback.
    """
    try:
        models = list(genai.list_models())
        for model in models:
            if (
                "generateContent" in model.supported_generation_methods
                and "gemini" in model.name.lower()
            ):
                return model.name
        for model in models:
            if "generateContent" in model.supported_generation_methods:
                return model.name
    except Exception:
        pass
    return "gemini-1.5-pro"


def generate_gemini_text(prompt: str, *, max_output_tokens: int = 500) -> str | None:
    """Freeform prompt -> freeform text via Gemini. None on any failure —
    callers should always have a deterministic fallback to show instead.
    """
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        return None
    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(_pick_model(genai))
        response = model.generate_content(
            prompt,
            generation_config={"max_output_tokens": max_output_tokens},
        )
        text = (response.text or "").strip()
        return text or None
    except Exception:
        logger.warning("Gemini call failed", exc_info=True)
        return None
