"""Torch public-form gate: only keee-torch-thc / torch-thc auto-approves."""

from types import SimpleNamespace

from events.torch_portal import (
    TORCH_RECAP_SUBMIT_CC,
    TORCH_REQUEST_APPROVED_CC,
    is_torch_public_form_slug,
    is_torch_tenant,
    should_auto_approve_public_request,
    torch_recap_submit_lists,
    torch_request_approved_lists,
)


def _tenant(**kwargs):
    defaults = {"slug": "", "request_url_name": "", "name": ""}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_only_keee_torch_thc_is_the_public_form():
    assert is_torch_public_form_slug("keee-torch-thc") is True
    assert is_torch_public_form_slug("KEEE-TORCH-THC") is True
    assert is_torch_public_form_slug("ighn-liquid-death") is False
    assert is_torch_public_form_slug("feel-free") is False
    assert is_torch_public_form_slug("girl-beer") is False
    assert is_torch_public_form_slug("kkc") is False
    assert is_torch_public_form_slug("") is False


def test_is_torch_tenant_matches_slug_url_and_name():
    assert is_torch_tenant(_tenant(slug="torch-thc")) is True
    assert is_torch_tenant(_tenant(slug="torch")) is True
    assert is_torch_tenant(_tenant(request_url_name="keee-torch-thc")) is True
    assert is_torch_tenant(_tenant(name="Torch THC")) is True
    assert is_torch_tenant(_tenant(slug="liquid-death")) is False
    assert is_torch_tenant(_tenant(slug="girl-beer", name="Girl Beer")) is False
    assert is_torch_tenant(_tenant(slug="feel-free")) is False
    assert is_torch_tenant(_tenant(slug="krispy-krunchy-chicken")) is False
    assert is_torch_tenant(None) is False


def test_auto_approve_requires_both_form_slug_and_torch_tenant():
    torch = _tenant(slug="torch-thc", request_url_name="keee-torch-thc", name="Torch THC")
    ld = _tenant(slug="liquid-death", request_url_name="ighn-liquid-death")
    assert should_auto_approve_public_request("keee-torch-thc", torch) is True
    # Tampered: Torch URL against another tenant stays pending.
    assert should_auto_approve_public_request("keee-torch-thc", ld) is False
    # Tampered: other form URL against Torch stays pending.
    assert should_auto_approve_public_request("ighn-liquid-death", torch) is False
    assert should_auto_approve_public_request("ighn-liquid-death", ld) is False
    assert should_auto_approve_public_request("girl-beer", _tenant(slug="girl-beer")) is False


def test_request_approved_lists_include_requestor_and_ops():
    to_emails, cc_emails = torch_request_approved_lists("buyer@store.com")
    assert to_emails == ["buyer@store.com"]
    lowered = {e.lower() for e in cc_emails}
    for expected in TORCH_REQUEST_APPROVED_CC:
        assert expected.lower() in lowered
    assert "girlbeer" not in " ".join(lowered)


def test_request_approved_lists_dedupe_requestor_on_cc():
    to_emails, cc_emails = torch_request_approved_lists("kyle@igniteproductions.co")
    assert to_emails == ["kyle@igniteproductions.co"]
    assert "kyle@igniteproductions.co" not in {e.lower() for e in cc_emails}


def test_recap_submit_lists_are_four_party_only():
    to_emails, cc_emails = torch_recap_submit_lists(["buyer@store.com"])
    assert to_emails == ["buyer@store.com"]
    lowered = {e.lower() for e in cc_emails}
    assert lowered == {e.lower() for e in TORCH_RECAP_SUBMIT_CC}
    for blast in (
        "kyle@igniteproductions.co",
        "harris@igniteproductions.co",
        "myriant@igniteproductions.co",
        "keis@igniteproductions.co",
    ):
        assert blast not in lowered
