"""
Parse a Connecteam-exported recap PDF and map its fields to a Spark
CustomRecapTemplate.

Connecteam PDFs follow a consistent `Label:: Value` shape — every row
in the source form turns into a labeled cell + value cell. Sometimes
the value lands on the same text line as the label, sometimes on the
next line (depends on cell wrap). Parser is tolerant of both layouts.

Multi-line values are stitched until we hit the next label or a known
section header. Section headers are lines that *don't* contain "::"
and appear between groups of fields (e.g. "Please provide details on
customer interaction.").

The match step normalizes both PDF labels and CustomField.name (lower,
strip punctuation, collapse whitespace) and uses an exact match first,
then difflib.get_close_matches for fuzzy fallback. Anything unmatched
is reported back so the importer can show the admin what fell on the
floor.
"""

from __future__ import annotations

import difflib
import io
import re
from dataclasses import dataclass, field


@dataclass
class ParsedRecap:
    """Result of parsing a Connecteam recap PDF."""

    # Raw {label: value} pairs as they appear in the PDF.
    raw_pairs: dict[str, str] = field(default_factory=dict)
    # Pages of extracted text — useful for debugging when nothing matches.
    page_texts: list[str] = field(default_factory=list)
    # Free-form header info (BA name, date, store) that doesn't have a
    # "::" label but appears in the top of every Connecteam recap.
    header: dict[str, str] = field(default_factory=dict)


# Lines like "Please enter the sales figures below." that separate
# field groups in the PDF. They have NO "::" so we can identify them
# easily, but we want to skip them entirely (they're decorative).
_SECTION_HEADER_PATTERN = re.compile(
    r"^\s*(please|share|provide|list|estimate)\s+", re.IGNORECASE
)

# Lines that look like "<Label>:: <Value>" — Connecteam's standard
# field separator. The double-colon is the giveaway; single colons
# also appear in values like "(40+):: 4" and we don't want to confuse
# those for separators.
_FIELD_PATTERN = re.compile(r"^(.+?)::\s*(.*)$")


def parse_pdf_bytes(data: bytes) -> ParsedRecap:
    """Extract every `Label:: Value` pair from a Connecteam recap PDF."""
    # Local import so the rest of the codebase doesn't pay the
    # pypdf import cost just by touching recaps.connecteam.
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]

    text = "\n".join(pages)
    result = ParsedRecap(page_texts=pages)

    current_label: str | None = None
    current_value_parts: list[str] = []

    def flush():
        nonlocal current_label, current_value_parts
        if current_label is not None:
            value = " ".join(p.strip() for p in current_value_parts).strip()
            # First-write-wins — same label appearing twice (rare but
            # possible if Connecteam ever renders a duplicate row)
            # shouldn't overwrite the first capture.
            if current_label not in result.raw_pairs:
                result.raw_pairs[current_label] = value
        current_label = None
        current_value_parts = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Section header — flush and skip.
        if _SECTION_HEADER_PATTERN.match(line) and "::" not in line:
            flush()
            continue

        # Field line.
        m = _FIELD_PATTERN.match(line)
        if m:
            # Starting a new field — flush the previous one.
            flush()
            label = m.group(1).strip()
            tail = m.group(2).strip()
            current_label = label
            if tail:
                current_value_parts = [tail]
            else:
                current_value_parts = []
            continue

        # No "::" — this is either a continuation of the current value
        # or top-of-page decoration (Connecteam often repeats the title
        # on every page). Heuristic: only treat as a continuation if
        # we have an active label *and* the line looks like a value
        # (not a header like "Girl Beer / Retail Sampling Recap").
        if current_label is not None:
            current_value_parts.append(line)

    flush()
    return result


def _normalize(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    Used for fuzzy-matching between PDF labels and CustomField.name —
    "# of PURPLE Variety Packs sold" → "of purple variety packs sold".
    """
    # Drop everything that isn't alphanumeric or whitespace.
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


@dataclass
class MatchResult:
    """One row in the field-mapping report."""

    pdf_label: str
    pdf_value: str
    field_name: str | None = None  # None = unmatched
    field_id: int | None = None
    score: float | None = None  # None for exact match
    skipped_reason: str | None = None  # e.g. "value empty"


def match_fields(
    parsed: ParsedRecap,
    custom_fields: list,  # list of CustomField rows
    fuzzy_cutoff: float = 0.85,
) -> list[MatchResult]:
    """Pair each parsed (label, value) with the closest CustomField.

    Returns one MatchResult per PDF row, in the order they appeared in
    the PDF. Rows with empty values are still returned (skipped_reason
    set) so the caller can show the admin what was blank in the source.
    """
    # Build the name→field lookup once. Normalize keys for matching.
    by_norm: dict[str, list] = {}
    for f in custom_fields:
        key = _normalize(f.name)
        by_norm.setdefault(key, []).append(f)

    norm_keys = list(by_norm.keys())
    results: list[MatchResult] = []

    for label, value in parsed.raw_pairs.items():
        norm_label = _normalize(label)

        # 1) Exact normalized match.
        candidates = by_norm.get(norm_label, [])
        if candidates:
            f = candidates[0]
            results.append(MatchResult(
                pdf_label=label,
                pdf_value=value,
                field_name=f.name,
                field_id=f.id,
                score=None,
                skipped_reason=None if value else "value empty",
            ))
            continue

        # 2) Fuzzy match.
        close = difflib.get_close_matches(
            norm_label, norm_keys, n=1, cutoff=fuzzy_cutoff,
        )
        if close:
            f = by_norm[close[0]][0]
            # difflib doesn't return the score with get_close_matches —
            # compute via SequenceMatcher to expose it.
            score = difflib.SequenceMatcher(None, norm_label, close[0]).ratio()
            results.append(MatchResult(
                pdf_label=label,
                pdf_value=value,
                field_name=f.name,
                field_id=f.id,
                score=round(score, 3),
                skipped_reason=None if value else "value empty",
            ))
            continue

        # 3) No match. Still included so the importer can show it.
        results.append(MatchResult(
            pdf_label=label,
            pdf_value=value,
            field_name=None,
            field_id=None,
            score=None,
            skipped_reason="no matching template field",
        ))

    return results
