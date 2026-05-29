"""
Coverage for the CustomRecap list-card aggregate derivations.

The web recaps LIST used to fetch the full customField + customRecapFiles
arrays for every row and derive the small card numbers client-side. Those
derivations now live server-side as scalar fields (soldUnits /
consumersSampled / heroImageUrl / customRecapFilesCount) so the list query
can drop the arrays. To guarantee the displayed numbers don't change, the
server math must replicate spark-front-client `SparkRecapsList.tsx`
byte-for-byte:

  * customSoldUnits   — SOLD = sum of every field whose NAME matches
                        /\\b(cans?|packs?)\\b/i; each value parsed by
                        stripping non [0-9-] then parseInt; null when no
                        field matched at all.
  * customConsumersSampled — first field whose NAME matches
                        /consumers?\\s+sampled/i, value parsed the same way;
                        null when none.
  * isImage           — /\\.(jpe?g|png|webp|gif)(\\?|$)/i

These tests exercise the pure derivation helpers directly (no DB) so the
exact regex/parse rules are pinned. Mirrors the "pure helper" style of
test_heic_display_url.py.
"""

from recaps.types import (
    _consumers_sampled_from_fields,
    _is_image_url,
    _parse_recap_int,
    _sold_units_from_fields,
)


# ---------------------------------------------------------------------------
# _parse_recap_int — mirrors parseInt(value.replace(/[^\d-]/g, ""), 10)
#                    + Number.isFinite
# ---------------------------------------------------------------------------


def test_parse_plain_integer():
    assert _parse_recap_int("42") == 42


def test_parse_strips_commas_and_words():
    # "1,234 cans" -> "1234" -> 1234
    assert _parse_recap_int("1,234 cans") == 1234


def test_parse_strips_currency_and_whitespace():
    assert _parse_recap_int("  $ 87 ") == 87


def test_parse_leading_zeros():
    assert _parse_recap_int("007") == 7


def test_parse_negative():
    assert _parse_recap_int("-5") == -5


def test_parse_stops_at_embedded_dash_like_js_parseint():
    # JS parseInt("12-34", 10) === 12 (stops at the dash).
    assert _parse_recap_int("12-34") == 12


def test_parse_dash_only_is_none():
    # parseInt("-", 10) is NaN -> Number.isFinite false -> skipped.
    assert _parse_recap_int("-") is None


def test_parse_non_numeric_is_none():
    assert _parse_recap_int("cans") is None


def test_parse_empty_is_none():
    assert _parse_recap_int("") is None


def test_parse_none_is_none():
    assert _parse_recap_int(None) is None


def test_parse_double_dash_is_none():
    # JS parseInt("--5", 10) is NaN.
    assert _parse_recap_int("--5") is None


# ---------------------------------------------------------------------------
# _sold_units_from_fields — customSoldUnits
# ---------------------------------------------------------------------------


def test_sold_units_sums_cans_and_packs():
    fields = [
        ("Single Cans", "100"),
        ("Packs Sold", "20"),
        ("Total Engagements", "500"),
    ]
    assert _sold_units_from_fields(fields) == 120


def test_sold_units_singular_and_plural_names_match():
    # "can" / "pack" (singular) and "Cans" / "Packs" (plural) all match.
    fields = [
        ("Can", "3"),
        ("Pack", "4"),
        ("Cans", "5"),
        ("Packs", "6"),
    ]
    assert _sold_units_from_fields(fields) == 18


def test_sold_units_word_boundary_excludes_substrings():
    # "Willing to purchase" must NOT match — \b(cans?|packs?)\b only hits
    # the whole word. "Backpacks" / "Cancellations" likewise excluded.
    fields = [
        ("Willing to purchase", "999"),
        ("Backpacks handed out", "10"),
        ("Cancellations", "7"),
    ]
    assert _sold_units_from_fields(fields) is None


def test_sold_units_none_when_no_matching_field():
    # No cans/packs field at all -> null (card shows "—", not 0).
    fields = [("Total Engagements", "500"), ("Photos", "12")]
    assert _sold_units_from_fields(fields) is None


def test_sold_units_ignores_non_numeric_values_but_keeps_matched():
    # A cans field with a numeric value sets matched; the packs field with
    # a non-numeric value contributes nothing but the total is still 50.
    fields = [("Cans", "50"), ("Packs", "n/a")]
    assert _sold_units_from_fields(fields) == 50


def test_sold_units_all_matching_values_unparseable_is_none():
    # Field NAMEs match but no value parses to a finite int -> matched
    # never set -> None (mirrors Number.isFinite gate on `matched`).
    fields = [("Cans", "n/a"), ("Packs", "-")]
    assert _sold_units_from_fields(fields) is None


def test_sold_units_parses_messy_values():
    fields = [("Cans", "1,000"), ("Packs", "  250 units ")]
    assert _sold_units_from_fields(fields) == 1250


def test_sold_units_empty_iterable_is_none():
    assert _sold_units_from_fields([]) is None


# ---------------------------------------------------------------------------
# _consumers_sampled_from_fields — customConsumersSampled
# ---------------------------------------------------------------------------


def test_consumers_sampled_basic():
    fields = [
        ("Cans", "100"),
        ("Total number of consumers sampled", "350"),
    ]
    assert _consumers_sampled_from_fields(fields) == 350


def test_consumers_sampled_singular_consumer():
    # "consumer sampled" (singular) also matches /consumers?\s+sampled/i.
    fields = [("Consumer Sampled", "42")]
    assert _consumers_sampled_from_fields(fields) == 42


def test_consumers_sampled_returns_first_match():
    fields = [
        ("Consumers Sampled", "10"),
        ("Consumers Sampled (verified)", "20"),
    ]
    assert _consumers_sampled_from_fields(fields) == 10


def test_consumers_sampled_none_when_no_match():
    fields = [("Cans", "100"), ("Packs", "20")]
    assert _consumers_sampled_from_fields(fields) is None


def test_consumers_sampled_none_when_value_unparseable():
    fields = [("Consumers Sampled", "lots")]
    assert _consumers_sampled_from_fields(fields) is None


def test_consumers_sampled_skips_unparseable_first_then_takes_next():
    # First matching field has a non-numeric value (parse -> None, skip),
    # second matching field parses -> returned. Mirrors the JS loop, which
    # only returns on a finite parse.
    fields = [
        ("Consumers Sampled", "n/a"),
        ("Consumers Sampled total", "275"),
    ]
    assert _consumers_sampled_from_fields(fields) == 275


# ---------------------------------------------------------------------------
# _is_image_url — frontend isImage
# ---------------------------------------------------------------------------


def test_is_image_url_common_extensions():
    base = "https://storage.googleapis.com/bucket/recap_files/photo"
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".JPG", ".PNG"):
        assert _is_image_url(base + ext) is True


def test_is_image_url_with_query_string():
    assert _is_image_url("https://cdn/img.png?token=abc") is True


def test_is_image_url_heic_is_false():
    # Frontend isImage deliberately excludes heic/heif.
    assert _is_image_url("https://cdn/IMG_1234.heic") is False
    assert _is_image_url("https://cdn/IMG_1234.heif") is False


def test_is_image_url_non_image_is_false():
    assert _is_image_url("https://cdn/report.pdf") is False
    assert _is_image_url("https://cdn/clip.mp4") is False


def test_is_image_url_none_is_false():
    assert _is_image_url(None) is False
    assert _is_image_url("") is False
