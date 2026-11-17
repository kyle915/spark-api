"""One way to read a number out of a recap answer.

Recap answers are text: a BA can type "1,234", " 40 ", "~30", "50+",
"20-30", "70 cans" or "about 12". Before this module, different paths read
the same answer differently: one stripped every non-digit ("1,234" -> 1234,
but "12.5" -> 125 and "sampled 50, sold 20" -> 5020), another took the first
run of digits ("1,234" -> 1). So the same recap could show 1,234 consumers
on one screen and 1 on another.

Every numeric read of a recap field value goes through :func:`parse_count`.

Rules (decided here, tested in ``recaps/tests/test_recap_numbers.py``):

* Thousands separators: "1,234" and "1,234,567" are grouped thousands. A
  space or non-breaking space between groups ("1 234") counts only when the
  answer is nothing but that number, so "3 120ml cans" is never 3,120. A
  comma that is not followed by exactly three digits is ambiguous ("12,5":
  twelve and a half, or 125?) and reads as None rather than a guess.
* Whitespace around the value is ignored.
* Approximate markers are ignored: a leading "~", "≈", "approx",
  "approximately", "about", "around", "roughly", "est"/"estimated", and a
  leading or trailing "+" ("50+" -> 50). The number counts as typed. A
  leading "$" is ignored too ("$ 87" -> 87).
* Ranges ("20-30", "20 – 30", "20 to 30") count as the LOWER bound. A
  client report should never claim more than the BA said; both readers
  before this module also stopped at the first number of a range.
* Decimals are truncated to a whole count ("12.5" -> 12; "1,234.9" -> 1234).
* Trailing words are allowed ("70 cans" -> 70, "12/25" -> 12), but the value
  has to START with the number. Prose ("We sampled 50 people") is not a
  count and reads as None, as do "N/A", "", "-" and "yes".
* A negative reads as negative ("-5" -> -5), as it always did on the recap
  list, so the review checks can still flag "a number below zero".

:func:`is_clean_count` is the strict form, for matchers that must not read
prose or guesses (the consumers-sampled count, staff-confirmed fields): the
whole answer is a whole number, optionally with "~", "≈", "$", "+" or a
range, and nothing else. Approximation WORDS ("about 30", "roughly 40 to
60"), decimals ("12.5") and negatives are not clean.
"""

from __future__ import annotations

import re

# A leading approximation word or sign. Order matters: longest first.
_APPROX = (
    r"(?:~|≈|approximately|approx\.?|about|around|roughly|"
    r"estimated|est\.?)"
)
# One number: comma-grouped thousands, else plain digits, with an optional
# decimal part. The (?!\d) after a grouped run stops "1,2345" from reading
# as 1,234 + "5".
_NUM = r"(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?"
# The whole answer is space-grouped thousands: "1 234", "12 345 678".
_SPACE_GROUPED_RE = re.compile(r"\s*\d{1,3}(?: \d{3})+\s*")
_RANGE_SEP = r"(?:-|–|—|to)"

_COUNT_RE = re.compile(
    rf"""^\s*
    (?:{_APPROX}\s*)?
    \$?\s*\+?\s*
    (?P<neg>-)?
    (?P<num>{_NUM})
    (?:\s*{_RANGE_SEP}\s*(?P<upper>{_NUM}))?
    \s*\+?
    (?P<rest>.*)$""",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

# The strict, whole-answer shape for is_clean_count: symbols only, whole
# numbers only.
_INT = r"(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)"
_CLEAN_RE = re.compile(
    rf"\s*[~≈]?\s*\$?\s*\+?\s*{_INT}(?:\s*{_RANGE_SEP}\s*{_INT})?\s*\+?\s*",
    re.IGNORECASE,
)

# Spaces that BAs paste from phones and spreadsheets.
_SPACE_LIKE = str.maketrans({" ": " ", " ": " ", " ": " "})


def _match(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).translate(_SPACE_LIKE)
    if _SPACE_GROUPED_RE.fullmatch(text):
        return int(text.replace(" ", ""))
    m = _COUNT_RE.match(text)
    if not m:
        return None
    rest = m.group("rest")
    # "1,2345" / "12.5.6" / "1 2 3": more digits glued on means the number
    # was not one of the shapes above. Don't guess.
    if re.match(r"[.,]?\d", rest):
        return None
    return m


def _to_int(num: str) -> int:
    whole = num.replace(",", "").split(".", 1)[0]
    return int(whole)


def parse_count(value) -> int | None:
    """The whole-number count in a recap answer, or None when it isn't one.

    See the module docstring for the rules.
    """
    m = _match(value)
    if m is None:
        return None
    if isinstance(m, int):
        return m
    n = _to_int(m.group("num"))
    return -n if m.group("neg") else n


def is_clean_count(value) -> bool:
    """True when the whole answer is a count, with nothing after it.

    "1,234", " 40 ", "~30", "50+", "20-30" are clean; "70 cans",
    "We sampled 50", "19 to mid-60s" and "" are not.
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    text = str(value).translate(_SPACE_LIKE)
    return bool(_SPACE_GROUPED_RE.fullmatch(text) or _CLEAN_RE.fullmatch(text))
