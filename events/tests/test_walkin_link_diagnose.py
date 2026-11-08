"""Diagnose payload shape for unmatched walk-up orphans."""

from events.walkin_link import WalkinLinkResult


def test_walkin_link_result_accepts_orphan_details():
    result = WalkinLinkResult(
        tenant_slug="famus-energy",
        apply=False,
        linked=0,
        would_link=0,
        ambiguous=0,
        orphans_with_filed=1,
        rows=[],
        orphan_details=[
            {
                "event_id": 1,
                "name": "Store",
                "reason": "no-same-day-due",
                "matched_due": [],
                "same_day_due_count": 0,
                "same_day_due": [],
            }
        ],
    )
    assert result.orphans_with_filed == 1
    assert result.orphan_details[0]["reason"] == "no-same-day-due"
