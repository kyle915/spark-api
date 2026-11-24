"""Shorten free text for display without cutting a word in half.

BA quotes and call-outs were clipped at a fixed character count
(``text[:n-1] + "…"``), so a client read "…the best atmosphere fo…". This
keeps whole words: it cuts at the last space inside the limit, drops
trailing punctuation that would sit oddly before the ellipsis, and only
falls back to a hard cut for a single word longer than the limit.
"""

from __future__ import annotations

ELLIPSIS = "…"


def clip_words(text: str | None, limit: int) -> str:
    """``text`` at most ``limit`` characters (ellipsis included), cut on a
    word boundary. Text already within the limit comes back unchanged."""
    if not text:
        return ""
    if limit <= 1 or len(text) <= limit:
        return text if len(text) <= max(limit, 0) else text[: max(limit, 0)]
    head = text[: limit - 1]
    # Cut at the last space only when it keeps a reasonable share of the text;
    # one enormous "word" (a URL) still gets a hard cut.
    space = head.rfind(" ")
    if space >= limit // 2:
        head = head[:space]
    return head.rstrip(" ,;:-–—.") + ELLIPSIS
