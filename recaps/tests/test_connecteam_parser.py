"""
Unit coverage for the Connecteam recap parser's label matching
(`recaps.connecteam`), focused on the Girl Beer "Retail Sampling Recap".

Background: a Girl Beer recap PDF imported with only "8 of 41 fields
recognized" because the tenant's CustomRecapTemplate didn't cover / didn't
label-match the Connecteam PDF labels (the template had drifted behind the
seed). `import_connecteam_recap_pdf` itself is correct — it matches the PDF
against the chosen template's CustomFields via
`match_fields(parsed, custom_fields)`. So the fix is twofold:
  * the seed (`onboard_girl_beer.SECTIONS`) now carries all ~41 fields with
    PDF-exact labels, and
  * the parser normalizes a bit more forgivingly (drops descriptive
    trailing parentheticals, lower fuzzy cutoff) WITHOUT cross-matching the
    near-identical demographic sibling rows.

These tests are pure-Python (no DB): `match_fields` only reads `.name`/`.id`
off each field, so we stand in lightweight fakes built straight from the
canonical seed spec. That keeps the "does the template cover the PDF?"
question honest — the test template IS the seed.
"""

from __future__ import annotations

from dataclasses import dataclass

from recaps.connecteam import ParsedRecap, _normalize, match_fields
from tenants.management.commands.onboard_girl_beer import SECTIONS


@dataclass
class _FakeField:
    """Minimal stand-in for a recaps.models.CustomField row.

    `match_fields` only touches `.name` and `.id`; nothing else is needed
    to exercise the normalize + exact + fuzzy matching path.
    """

    id: int
    name: str


def _template_fields() -> list[_FakeField]:
    """The full Girl Beer template, straight from the seed's source of
    truth so this test tracks any future field additions automatically."""
    fields: list[_FakeField] = []
    for _section_name, section_fields in SECTIONS:
        for field_name, _ftype, _required in section_fields:
            fields.append(_FakeField(id=len(fields) + 1, name=field_name))
    return fields


# The ground-truth Connecteam "Retail Sampling Recap" labels exactly as
# they render in the export (Brand Ambassador / Date / Store-Location are
# event-level, not template fields, so they're not here). ~41 source
# questions; the age-bracket / (Total) cells are one label each → 45 rows.
GIRL_BEER_PDF_LABELS = [
    "Store Associate Spoken To",
    "What flavors were available to taste?",
    "# of PURPLE Variety Packs sold",
    "# of RED Variety Packs sold",
    "Blueberry Lavender 6-packs Sold",
    "Pineapple Yuzu 6-packs Sold",
    "Grapefruit Guava 6-packs Sold",
    "Peach 6-packs Sold",
    "Tangerine 6-packs Sold",
    "Total Samples Given Out",
    "Foot Traffic (number of people walking by demo table per hour)",
    "Number of Customers Engaged (talked to or sampled product)",
    "Men who bought (21-29)",
    "Men who bought (30-39)",
    "Men who bought (40+)",
    "Men who bought (Total)",
    "Women who bought (21-29)",
    "Women who bought (30-39)",
    "Women who bought (40+)",
    "Women who bought (Total)",
    "Men who sampled (21-29)",
    "Men who sampled (30-39)",
    "Men who sampled (40+)",
    "Men who sampled (Total)",
    "Women who sampled (21-29)",
    "Women who sampled (30-39)",
    "Women who sampled (40+)",
    "Women who sampled (Total)",
    "Total sampled (21-29)",
    "Total sampled (30-39)",
    "Total sampled (40+)",
    "Total sampled (Total)",
    "Most Common Question / Comment 1",
    "Most Common Question / Comment 2",
    "Most Common Question / Comment 3",
    "Most Common Question / Comment 4",
    "Positive Feedback From Customers",
    "Negative Feedback / Concerns From Customers",
    "How was the setup?",
    "Did the demo influence the store to place a reorder?",
    "Anything that could make future demos better?",
    "Account Spend Amount",
    "Product purchase receipt (image)",
    "Table setup pictures",
    "Sampling pictures (photos)",
]


def _parsed_from_labels(labels) -> ParsedRecap:
    """Build a ParsedRecap whose raw_pairs are the given labels with dummy
    non-empty values (so nothing is skipped as 'value empty')."""
    return ParsedRecap(raw_pairs={label: "1" for label in labels})


# --------------------------------------------------------------------------
# Normalization behavior — the crux of the fix.
# --------------------------------------------------------------------------

class TestNormalize:
    def test_drops_descriptive_trailing_parenthetical(self):
        # Pure flavor text gets peeled so the PDF label exact-matches a
        # shorter template field.
        assert _normalize("Sampling pictures (photos)") == "sampling pictures"
        assert _normalize("Product purchase receipt (image)") == (
            "product purchase receipt"
        )
        assert _normalize(
            "Number of Customers Engaged (talked to or sampled product)"
        ) == "number of customers engaged"

    def test_foot_traffic_label_drift_collapses_to_same_key(self):
        # The old drifted label and the PDF-exact label must normalize to
        # the same thing so an un-repaired template still matches.
        a = _normalize("Foot Traffic (number of people walking by demo table per hour)")
        b = _normalize("Foot Traffic (people walking by per hour)")
        assert a == b == "foot traffic"

    def test_keeps_discriminating_age_bracket_parenthetical(self):
        # Age brackets MUST survive or the four bought/sampled rows collide.
        assert _normalize("Men who bought (21-29)") == "men who bought 21 29"
        assert _normalize("Men who bought (30-39)") == "men who bought 30 39"
        assert _normalize("Men who bought (40+)") == "men who bought 40"
        assert _normalize("Men who bought (Total)") == "men who bought total"
        # ...and they are all distinct from each other.
        variants = {
            _normalize(f"Men who bought ({b})")
            for b in ("21-29", "30-39", "40+", "Total")
        }
        assert len(variants) == 4

    def test_dollar_suffix_equals_bare_money_label(self):
        assert _normalize("Account Spend Amount ($)") == "account spend amount"
        assert _normalize("Account Spend Amount") == "account spend amount"


# --------------------------------------------------------------------------
# Full-template matching — the regression this whole change exists to fix.
# --------------------------------------------------------------------------

class TestGirlBeerTemplateMatching:
    def test_full_template_recognizes_at_least_38_of_the_pdf_labels(self):
        """The headline assertion: feeding the real Girl Beer PDF labels at
        the full seed template recognizes ≥38 (was 8 against the drifted
        live template). In practice every label lands."""
        fields = _template_fields()
        parsed = _parsed_from_labels(GIRL_BEER_PDF_LABELS)

        results = match_fields(parsed, fields)
        recognized = [r for r in results if r.field_id is not None]

        assert len(results) == len(GIRL_BEER_PDF_LABELS)
        assert len(recognized) >= 38, (
            f"only {len(recognized)}/{len(GIRL_BEER_PDF_LABELS)} recognized; "
            f"unmatched: {[r.pdf_label for r in results if r.field_id is None]}"
        )
        # The full seed should actually cover ALL of them.
        assert len(recognized) == len(GIRL_BEER_PDF_LABELS)

    def test_no_cross_field_mismatch_among_sibling_rows(self):
        """Every label must map to the field with the SAME name (exact),
        not bleed into an adjacent age-bracket / gender sibling."""
        fields = _template_fields()
        field_names = {f.name for f in fields}
        parsed = _parsed_from_labels(GIRL_BEER_PDF_LABELS)

        results = match_fields(parsed, fields)
        for r in results:
            assert r.field_name is not None, f"unmatched: {r.pdf_label!r}"
            # Since the seed mirrors the PDF labels verbatim, each should be
            # an exact (score is None) self-match.
            assert r.field_name in field_names
            assert r.field_name == r.pdf_label, (
                f"{r.pdf_label!r} cross-matched to {r.field_name!r}"
            )

    def test_each_demographic_total_row_lands_on_its_own_field(self):
        """Guard the most collision-prone family explicitly: the four
        '(Total)' rows must each hit their own field, never a (40+) sibling
        or the other gender's total."""
        fields = _template_fields()
        parsed = _parsed_from_labels(GIRL_BEER_PDF_LABELS)
        by_label = {r.pdf_label: r for r in match_fields(parsed, fields)}

        for label in (
            "Men who bought (Total)",
            "Women who bought (Total)",
            "Men who sampled (Total)",
            "Women who sampled (Total)",
            "Total sampled (Total)",
        ):
            assert by_label[label].field_name == label, by_label[label]

    def test_drifted_template_regression_floor(self):
        """Documents the 'before': a template that is MISSING the four
        (Total) bought/sampled rows, the whole 'Total sampled' group, and
        carries the drifted Foot-Traffic / Customers-Engaged labels.

        It should recognize meaningfully FEWER than the full template (the
        Total-sampled group has no field to land on), proving the fix is
        what closes the gap — while still confirming the parser's
        forgiveness recovers the renamed labels via normalization."""
        drifted_names = []
        skip = {
            "Men who bought (Total)",
            "Women who bought (Total)",
            "Men who sampled (Total)",
            "Women who sampled (Total)",
            "Total sampled (21-29)",
            "Total sampled (30-39)",
            "Total sampled (40+)",
            "Total sampled (Total)",
        }
        rename_back = {
            "Foot Traffic (number of people walking by demo table per hour)":
                "Foot Traffic (people walking by per hour)",
            "Number of Customers Engaged (talked to or sampled product)":
                "Number of Customers Engaged",
            "Product purchase receipt (image)": "Product purchase receipt",
            "Sampling pictures (photos)": "Sampling pictures",
            "Account Spend Amount": "Account Spend Amount ($)",
        }
        for _section, section_fields in SECTIONS:
            for name, _ft, _req in section_fields:
                if name in skip:
                    continue
                drifted_names.append(rename_back.get(name, name))
        drifted_fields = [
            _FakeField(id=i + 1, name=n) for i, n in enumerate(drifted_names)
        ]

        parsed = _parsed_from_labels(GIRL_BEER_PDF_LABELS)
        recognized = [
            r for r in match_fields(parsed, drifted_fields)
            if r.field_id is not None
        ]
        # The 'Total sampled' group (4 labels) has no home on the drifted
        # template, so we must lose at least those.
        assert len(recognized) <= len(GIRL_BEER_PDF_LABELS) - 4
        # And the full template strictly beats it — that's the fix.
        full = [
            r for r in match_fields(parsed, _template_fields())
            if r.field_id is not None
        ]
        assert len(full) > len(recognized)
