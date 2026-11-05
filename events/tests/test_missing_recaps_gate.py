"""Famus Missing Recaps gate — feature off for famus-energy only."""

from events.missing_recaps_gate import (
    missing_recaps_disabled_for_slug,
    missing_recaps_disabled_for_tenant,
)


def test_famus_slug_disables_missing_recaps():
    assert missing_recaps_disabled_for_slug("famus-energy") is True
    assert missing_recaps_disabled_for_slug("FAMUS-ENERGY") is True
    assert missing_recaps_disabled_for_slug("liquid-death") is False
    assert missing_recaps_disabled_for_slug("") is False
    assert missing_recaps_disabled_for_slug(None) is False


def test_tenant_object_uses_slug_or_request_url_name():
    class T:
        slug = None
        request_url_name = "famus-energy"

    assert missing_recaps_disabled_for_tenant(T()) is True

    class Other:
        slug = "wook-water"
        request_url_name = "wook-water"

    assert missing_recaps_disabled_for_tenant(Other()) is False
    assert missing_recaps_disabled_for_tenant(None) is False
