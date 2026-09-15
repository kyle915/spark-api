"""The approval email must not print placeholder rows it has no value for.

Walk-up recaps have no scheduled window, no clock records, no approved
booking and no shift extension, so five rows in the client email rendered
as "- - -", "-", "-", "-" and "—". They are dropped now — but ONLY when
empty: a scheduled event with a booked BA and an approved extension still
shows every one of them.
"""
from django.template.loader import render_to_string

import pytest

TEMPLATES = [
    "emails/custom_recap_approved_notification.html",
    "emails/recap_approved_notification.html",
]
LABELS = [
    "Scheduled Time",
    "Actual Check-In",
    "Actual Check-Out",
    "BA(s) On-Site",
    "Extensions",
]

BASE = {
    "recipient_first_name": "Kyle",
    "request_id": "REQ-1",
    "brand_name": "Torch THC",
    "campaign_name": "Retail Sampling",
    "location_name": "Binny's",
    "date_text": "09/11/2026",
    "photos_count": 4,
    "client_specific_metrics": [],
    "recap_link": "https://example.test/r/1",
}

EMPTY = {
    **BASE,
    "scheduled_start_time": "",
    "scheduled_end_time": "",
    "actual_check_in": "",
    "actual_check_out": "",
    "ba_on_site": 0,
    "extensions_text": "",
}

FULL = {
    **BASE,
    "scheduled_start_time": "3:00 PM",
    "scheduled_end_time": "6:00 PM",
    "actual_check_in": "2:58 PM",
    "actual_check_out": "6:04 PM",
    "ba_on_site": 2,
    "extensions_text": "1h 30m",
}


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_empty_rows_are_dropped(tpl):
    html = render_to_string(tpl, EMPTY)
    for label in LABELS:
        assert label not in html, f"{label} rendered despite having no value"


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_populated_rows_still_render(tpl):
    html = render_to_string(tpl, FULL)
    for label in LABELS:
        assert label in html, f"{label} disappeared even though it has a value"
    assert "3:00 PM" in html and "6:00 PM" in html
    assert "2:58 PM" in html and "6:04 PM" in html
    assert "1h 30m" in html


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_rows_that_always_matter_are_untouched(tpl):
    """Brand / location / date must survive both paths."""
    for ctx in (EMPTY, FULL):
        html = render_to_string(tpl, ctx)
        assert "Torch THC" in html
        # Django autoescapes the apostrophe in the store name.
        assert "Binny&#x27;s" in html or "Binny's" in html
        assert "09/11/2026" in html


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_no_placeholder_dashes_left_in_the_table(tpl):
    """The specific symptom: a table cell whose whole content is a dash."""
    html = render_to_string(tpl, EMPTY)
    for junk in (">- - -<", ">-<", ">—<"):
        assert junk not in html, f"placeholder {junk!r} still rendered"
