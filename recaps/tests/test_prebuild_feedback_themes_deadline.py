"""prebuild_feedback_themes must stop before Cloud Run's 300 s request limit.

The deadline used to be checked as "already past it?" before each range, but
one range can make three model calls, so a range started a few seconds before
the deadline ran the HTTP request into a 504.
"""
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from recaps.management.commands import prebuild_feedback_themes as cmd

CMD = "recaps.management.commands.prebuild_feedback_themes"


class _Clock:
    """monotonic() that moves forward only when a range is built."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _run(clock, *, max_seconds, seconds_per_range):
    built: list = []

    def build(tenant_id, start, end):
        built.append((start, end))
        clock.now += seconds_per_range
        return {"status": "ready"}

    out = StringIO()
    with (
        patch(f"{CMD}.time.monotonic", clock),
        patch("recaps.feedback_themes.gather_comments", return_value=(["c"] * 50, False)),
        patch("recaps.feedback_themes.source_hash", return_value="h"),
        patch("recaps.feedback_themes.get_feedback_themes", side_effect=build),
        patch("recaps.comment_insights.get_competitor_mentions"),
        patch("recaps.comment_insights.get_product_regions"),
    ):
        call_command(
            "prebuild_feedback_themes", "--tenant", "1",
            "--max-seconds", str(max_seconds), stdout=out,
        )
    return built, out.getvalue()


@pytest.mark.django_db
def test_no_range_starts_with_less_than_the_headroom_left():
    clock = _Clock()
    # 150 s budget, 40 s per range: ranges start at 0, 40, 80 s left
    # 150, 110, 70 → a 4th would start with 30 s left, under the 60 s headroom.
    built, log = _run(clock, max_seconds=150, seconds_per_range=40)
    assert len(built) == 3
    assert "time limit reached" in log


@pytest.mark.django_db
def test_quick_ranges_all_run():
    clock = _Clock()
    built, log = _run(clock, max_seconds=150, seconds_per_range=1)
    assert len(built) == len(cmd.ranges_for(cmd.date.today()))
    assert "time limit reached" not in log


def test_default_budget_fits_inside_cloud_runs_limit():
    parser = cmd.Command().create_parser("manage.py", "prebuild_feedback_themes")
    default = parser.parse_args([]).max_seconds
    assert default <= 150
