"""Famus Missing Recaps gate — lifted; accepted orphans parked."""

from events.missing_recaps_gate import (
    ACCEPTED_WALKUP_ORPHAN_EVENT_IDS,
    MISSING_RECAPS_DISABLED_SLUGS,
    missing_recaps_disabled_for_slug,
    missing_recaps_disabled_for_tenant,
)


def test_famus_slug_no_longer_disables_missing_recaps():
    assert "famus-energy" not in MISSING_RECAPS_DISABLED_SLUGS
    assert missing_recaps_disabled_for_slug("famus-energy") is False
    assert missing_recaps_disabled_for_slug("liquid-death") is False
    assert missing_recaps_disabled_for_slug("") is False
    assert missing_recaps_disabled_for_slug(None) is False


def test_tenant_object_famus_enabled():
    class T:
        slug = None
        request_url_name = "famus-energy"

    assert missing_recaps_disabled_for_tenant(T()) is False

    class Other:
        slug = "wook-water"
        request_url_name = "wook-water"

    assert missing_recaps_disabled_for_tenant(Other()) is False
    assert missing_recaps_disabled_for_tenant(None) is False


def test_accepted_orphans_parked():
    assert ACCEPTED_WALKUP_ORPHAN_EVENT_IDS == frozenset(
        {6948, 6988, 7021, 7039}
    )
