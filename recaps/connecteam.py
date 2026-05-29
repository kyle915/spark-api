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
class ParsedImage:
    """One image extracted from a PDF page.

    `preceding_label` is the closest "Label::" line that came right
    before this image in the PDF — useful for telling sampling photos
    apart from receipt photos when the admin previews the import.
    """

    bytes_: bytes
    extension: str  # ".png" / ".jpg" / ".jpeg"
    page_index: int  # 0-based
    image_index: int  # within the page
    preceding_label: str | None = None


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
    # Images embedded in the PDF, with the label of the field they
    # most likely belong to (computed from text position on the page).
    images: list[ParsedImage] = field(default_factory=list)


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

# Fallback for Connecteam PDFs that render with single colons (newer
# template versions strip the `::` Nevena's testing surfaced one such
# PDF where every label ended in a single `:`). We only use this
# fallback when the strict double-colon pass found ZERO matches —
# otherwise it would catch every "Total: 42" appearing inside a
# value. The trailing-colon requirement plus a length cap on the
# label (under 120 chars) keeps it from devouring sentences.
_FIELD_PATTERN_FALLBACK = re.compile(r"^(.{1,120}?):\s*(.*)$")


def parse_pdf_bytes(data: bytes) -> ParsedRecap:
    """Extract every `Label:: Value` pair from a Connecteam recap PDF,
    and pull out any embedded page images so the importer can attach
    them to the resulting CustomRecap as CustomRecapFile rows."""
    # Local import so the rest of the codebase doesn't pay the
    # pypdf import cost just by touching recaps.connecteam.
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages_objs = list(reader.pages)
    pages = [p.extract_text() or "" for p in pages_objs]

    text = "\n".join(pages)
    result = ParsedRecap(page_texts=pages)

    # Walk page-by-page to pull images. The last "Label::" we saw on
    # the same page is the most likely owner of any image that
    # follows — Connecteam renders image upload rows as "<Label>::"
    # immediately above the image cell.
    for page_idx, page in enumerate(pages_objs):
        page_text = pages[page_idx]
        # Find the last label line on this page; we'll use it as the
        # preceding label for any image extracted from this page.
        last_label_on_page: str | None = None
        for line in reversed(page_text.splitlines()):
            stripped = line.strip()
            m = _FIELD_PATTERN.match(stripped)
            if m:
                last_label_on_page = m.group(1).strip()
                break
        try:
            page_images = list(page.images)
        except Exception:
            # Some PDFs have malformed image streams — skip gracefully.
            page_images = []
        for img_idx, image_obj in enumerate(page_images):
            try:
                blob = bytes(image_obj.data)
            except Exception:
                continue
            ext = _ext_from_image(image_obj)
            result.images.append(ParsedImage(
                bytes_=blob,
                extension=ext,
                page_index=page_idx,
                image_index=img_idx,
                preceding_label=last_label_on_page,
            ))

    _extract_pairs(text, result, _FIELD_PATTERN)

    # Connecteam fallback — if the strict pass found nothing, try
    # single-colon labels. This catches the newer template variants
    # that stopped using `::` (Nevena's Trevor Simmons recap was the
    # first PDF we saw with this shape).
    if not result.raw_pairs:
        _extract_pairs(text, result, _FIELD_PATTERN_FALLBACK)

    return result


def _extract_pairs(text: str, result: "ParsedRecap", pattern: re.Pattern) -> None:
    """Walk every line of `text`, populate `result.raw_pairs` using
    the given label pattern. Stateful: when a `Label:` line appears,
    the next non-label, non-section-header lines are appended to the
    current value until the next label arrives."""
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
        if _SECTION_HEADER_PATTERN.match(line) and ":" not in line:
            flush()
            continue

        # Field line.
        m = pattern.match(line)
        if m:
            # Starting a new field — flush the previous one.
            flush()
            label = m.group(1).strip()
            tail = m.group(2).strip()
            # Reject obvious false positives: "URL: http..." style
            # values, time stamps "07:10 PM", "10:00 - 14:00", etc.
            # Labels typically don't have leading digits.
            if label and label[0].isdigit():
                if current_label is not None:
                    current_value_parts.append(line)
                continue
            current_label = label
            if tail:
                current_value_parts = [tail]
            else:
                current_value_parts = []
            continue

        # No label — this is either a continuation of the current value
        # or top-of-page decoration (Connecteam often repeats the title
        # on every page). Heuristic: only treat as a continuation if
        # we have an active label *and* the line looks like a value
        # (not a header like "Girl Beer / Retail Sampling Recap").
        if current_label is not None:
            current_value_parts.append(line)

    flush()


def _ext_from_image(image_obj) -> str:
    """Pick a sensible file extension for an image extracted by pypdf.

    pypdf's `Page.images` items expose `.name` (e.g. "/Im1.jpg") and
    sometimes a sniffable `.image` PIL object. Fall back to `.png` if
    nothing tips us off — most browsers will still render correctly
    based on content type detection at upload time.
    """
    name = getattr(image_obj, "name", None) or ""
    if "." in name:
        ext = name.rsplit(".", 1)[-1].lower().strip("/")
        if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
            return f".{ext}"
    # Sniff via PIL if available.
    pil = getattr(image_obj, "image", None)
    fmt = getattr(pil, "format", "") if pil else ""
    if fmt:
        fmt = fmt.lower()
        if fmt in {"png", "jpg", "jpeg", "webp", "gif"}:
            return f".{fmt if fmt != 'jpeg' else 'jpg'}"
    return ".png"


# A trailing "(...)" with no nested parens. We peel these off a label
# BEFORE alphanumeric stripping, but ONLY when the parenthetical is
# purely descriptive noise — never when it carries a token that
# distinguishes one sibling field from another.
_TRAILING_PAREN = re.compile(r"\s*\(([^()]*)\)\s*$")


def _is_discriminating_paren(inner: str) -> bool:
    """True when a trailing parenthetical distinguishes sibling fields and
    therefore MUST be preserved through normalization.

    The Girl Beer recap has families of near-identical labels that differ
    only by what's in the parentheses — age brackets and a roll-up row:

        "Men who bought (21-29)" / "(30-39)" / "(40+)" / "(Total)"

    If we dropped these we'd collapse the whole family to "men who bought"
    and the four values would fight over one field. So we keep any
    parenthetical that contains a digit (age brackets like 21-29 / 40+)
    or the word "total" (the roll-up row). Everything else — "(image)",
    "(photos)", "(talked to or sampled product)", "(people walking by per
    hour)" — is descriptive flavor text that only causes label drift, so
    it gets stripped to maximize exact-match hits.
    """
    low = inner.lower()
    return bool(re.search(r"\d", low)) or "total" in low


def _normalize(name: str) -> str:
    """Lowercase, drop descriptive parenthetical noise, drop punctuation,
    collapse whitespace.

    Used for fuzzy-matching between PDF labels and CustomField.name —
    "# of PURPLE Variety Packs sold" → "of purple variety packs sold".

    Trailing descriptive parentheticals are peeled first so minor label
    drift between the PDF and the template still lands an EXACT match:
    "Sampling pictures (photos)" and a "Sampling pictures" field both
    normalize to "sampling pictures"; "Foot Traffic (number of people
    walking by demo table per hour)" and "Foot Traffic (people walking by
    per hour)" both collapse to "foot traffic". Discriminating
    parentheticals (age brackets, "(Total)") are kept so sibling
    demographic rows never collide — see `_is_discriminating_paren`.
    """
    s = name.strip()
    # Peel trailing descriptive parentheticals (possibly several), but
    # stop the moment we hit one that distinguishes sibling fields.
    while True:
        m = _TRAILING_PAREN.search(s)
        if not m or _is_discriminating_paren(m.group(1)):
            break
        s = s[: m.start()]
    # Drop everything that isn't alphanumeric or whitespace.
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", s.lower())
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
    fuzzy_cutoff: float = 0.80,
) -> list[MatchResult]:
    """Pair each parsed (label, value) with the closest CustomField.

    Returns one MatchResult per PDF row, in the order they appeared in
    the PDF. Rows with empty values are still returned (skipped_reason
    set) so the caller can show the admin what was blank in the source.

    `fuzzy_cutoff` defaults to 0.80 (was 0.85): the normalization above
    already absorbs the common drift (descriptive parentheticals), so
    the remaining fuzzy budget is spent on small wording differences
    ("6-packs" vs "6 packs", a stray typo). The discriminating-token
    rule in `_normalize` keeps the age-bracket / "(Total)" sibling rows
    far enough apart that this lower cutoff doesn't cross-match them
    when the matching field is present (the normal case once the
    template covers every PDF label).
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


def route_single_label_images(image_blobs, image_fields) -> dict:
    """Route a Connecteam-imported image to an IMAGE CustomField's VALUE — but
    ONLY when exactly one image's preceding label exactly-normalizes to that
    field's name.

    Deliberately narrow: it handles the single-image "Product purchase
    receipt" case and nothing riskier. The receipt's "Product purchase
    receipt::" PDF label normalizes to the "Product purchase receipt (image)"
    field (the descriptive "(image)" parenthetical is peeled by `_normalize`),
    a clean 1:1. Multi-image fields (sampling / table-setup photos) either
    don't match a field name exactly or have more than one matching image, so
    the exactly-one rule skips them — they stay in the flat CustomRecapFile
    gallery (no data loss, no regression of the flat-gallery behavior).

    Exact-normalized (not fuzzy) on purpose, to avoid cross-field mis-routing
    from the fragile preceding-label signal.

    Args:
        image_blobs: list of (ParsedImage, stored_blob_path) for attached imgs.
        image_fields: CustomField rows whose type is IMAGE.
    Returns:
        {custom_field_id: blob_path} for fields with exactly one label match.
    """
    routing: dict = {}
    for field in image_fields:
        fnorm = _normalize(getattr(field, "name", "") or "")
        if not fnorm:
            continue
        matches = [
            blob
            for (img, blob) in image_blobs
            if img.preceding_label and _normalize(img.preceding_label) == fnorm
        ]
        if len(matches) == 1:
            routing[int(field.id)] = matches[0]
    return routing


# --------------------------------------------------------------------------
# Legacy-form mapping
#
# The CustomRecapTemplate path (match_fields, above) maps a PDF onto a
# tenant's custom field schema. The STANDARD recap form (SparkRecapCreate
# on the admin web) is a fixed grid of well-known fields instead —
# samples, first-time, brand-aware, willing/not-willing, cans, packs,
# products sold, account spend, plus free-text traffic / competitive /
# quotes / feedback. `map_legacy_fields` does a best-effort label match
# onto THOSE fields so the admin can drop a Connecteam PDF on the create
# form and have the numbers pre-filled for review.
#
# This is intentionally heuristic and forgiving: the caller always
# returns the raw label/value pairs too, the admin reviews every value
# before submitting, and nothing is written to the DB by the parse step.
# Unrecognized labels are simply left for the admin to enter by hand.
# --------------------------------------------------------------------------

# Legacy fields whose value is a count (summed across matching rows, so a
# PDF that lists cans per-SKU still totals correctly). Everything else is
# either money (account_spend) or free text.
_LEGACY_NUMERIC_KEYS = frozenset({
    "total_consumer",
    "first_time",
    "brand_aware",
    "willing",
    "not_willing",
    "products_sold",
    "total_cans_sold",
    "total_packs_sold",
})

# Money — keep the decimal, don't sum (account spend is a single figure).
_LEGACY_MONEY_KEY = "account_spend"

_MONEY_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _leading_money(value) -> str | None:
    """Pull the first money-ish number out of a value, stripping commas.
    '$1,250.50' -> '1250.50', '40' -> '40'. None when there's no number."""
    if value is None:
        return None
    m = _MONEY_RE.search(str(value))
    return m.group().replace(",", "") if m else None


def _classify_legacy_label(label: str) -> str | None:
    """Map a (lower-cased) Connecteam label onto a legacy recap form key,
    or None if nothing reasonable matches. Order matters — the most
    specific tests come first (e.g. 'not willing' before 'willing',
    'pack' before the generic 'products sold')."""
    # --- consumer engagement ---
    if "consumers sampled" in label or ("sampled" in label and "consumer" in label):
        return "total_consumer"
    if "first time" in label or "first-time" in label:
        return "first_time"
    if "knew about" in label or "brand aware" in label or "aware of" in label:
        return "brand_aware"
    if "willing to purchase" in label or "willing to buy" in label:
        # "would NOT be willing to purchase" -> not_willing
        return "not_willing" if "not" in label else "willing"

    # --- sales (check pack/cans before the generic 'products sold') ---
    if "multipack" in label or "multi-pack" in label or "multi pack" in label or "pack" in label:
        return "total_packs_sold"
    if "cans" in label or "can sold" in label:
        return "total_cans_sold"
    if "products sold" in label or "product sold" in label or "total sold" in label or "units sold" in label:
        return "products_sold"

    # --- money ---
    if "account spend" in label or "amount spent" in label or "spend" in label or "total spend" in label:
        return "account_spend"

    # --- free text ---
    if "traffic" in label:
        return "traffic_description"
    if "competit" in label:
        return "competitive_presence"
    if "quote" in label:
        return "quotes"
    if "demographic" in label:
        return "demographics"
    if "positive" in label or "success story" in label:
        return "positive_stories"
    if "decline" in label or "objection" in label or ("reason" in label and "not" in label):
        return "reasons_to_decline"
    if "differently" in label or "improve" in label or "next time" in label:
        return "do_differently"
    if "feedback" in label:
        return "feedback"
    if "comment" in label or "additional" in label or "note" in label:
        return "account_notes"
    return None


def map_legacy_fields(parsed: "ParsedRecap") -> tuple[dict[str, str], int]:
    """Best-effort map of a parsed Connecteam recap onto the standard
    recap form's fields. Returns (fields, matched_count) where `fields`
    is {legacy_key: str_value} ready to drop into the create form.

    Numeric fields are summed across matching rows; free-text fields are
    joined with newlines; account spend takes the last money value seen.
    Empty values and unrecognized labels are skipped."""
    out: dict[str, str] = {}
    matched = 0
    for label_raw, value in parsed.raw_pairs.items():
        if value is None or str(value).strip() == "":
            continue
        key = _classify_legacy_label(label_raw.lower().strip())
        if key is None:
            continue
        if key in _LEGACY_NUMERIC_KEYS:
            n = _leading_int(value)
            if n is None:
                continue
            prev = int(out.get(key) or 0)
            out[key] = str(prev + n)
        elif key == _LEGACY_MONEY_KEY:
            money = _leading_money(value)
            if money is None:
                continue
            out[key] = money
        else:  # free text
            text = str(value).strip()
            out[key] = (out[key] + "\n" + text) if key in out else text
        matched += 1
    return out, matched


def _leading_int(value) -> int | None:
    """Pull a non-negative integer out of a value ('70', '70 cans',
    '  68 '). None when there's no number. (Mirrors recaps.pdf so the
    legacy mapping doesn't depend on importing the PDF module.)"""
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None
