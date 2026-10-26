"""Torch public-form gate: only keee-torch-thc / torch-thc auto-approves."""

from types import SimpleNamespace

from events.field_marketing_mail import TORCH_PLAN_SUBMIT_INTERNAL_EMAILS
from events.request_auto_approve import FAMUS_EVENT_SUBMISSION_SKIP_EMAILS
from events.routing import ALWAYS_INCLUDE_INTERNAL_EMAILS
from events.torch_portal import (
    TORCH_RECAP_SKIP_EMAILS,
    TORCH_RECAP_SUBMIT_CC,
    TORCH_REQUEST_APPROVED_CC,
    TORCH_SUPPRESS_EMAILS,
    is_torch_public_form_slug,
    is_torch_tenant,
    omit_torch_recap_skips,
    should_auto_approve_public_request,
    suppress_torch_emails,
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
    for standing in ALWAYS_INCLUDE_INTERNAL_EMAILS:
        assert standing in lowered
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


def test_baesha_is_suppressed_from_torch_emails_only():
    torch = _tenant(slug="torch-thc", request_url_name="keee-torch-thc", name="Torch THC")
    famus = _tenant(slug="famus-energy", request_url_name="famus-energy", name="FAMUS Energy")
    raw = ["Baesha@igniteproductions.co", "kyle@igniteproductions.co"]

    assert "baesha@igniteproductions.co" in TORCH_SUPPRESS_EMAILS
    assert suppress_torch_emails(raw, tenant=torch) == ["kyle@igniteproductions.co"]
    assert suppress_torch_emails(raw) == ["kyle@igniteproductions.co"]
    assert suppress_torch_emails(raw, tenant=famus) == raw

    to_emails, cc_emails = torch_request_approved_lists("Baesha@igniteproductions.co")
    assert "baesha@igniteproductions.co" not in {e.lower() for e in cc_emails}
    to_recap, cc_recap = torch_recap_submit_lists(["Baesha@igniteproductions.co", "buyer@store.com"])
    assert to_recap == ["buyer@store.com"]
    assert "baesha@igniteproductions.co" not in {e.lower() for e in cc_recap}


def test_junior_is_skipped_on_torch_recap_mail_only():
    shared = [
        "Junior@igniteproductions.co",
        "kyle@igniteproductions.co",
        *ALWAYS_INCLUDE_INTERNAL_EMAILS,
    ]
    torch_slugs = (
        _tenant(slug="torch", name="Torch"),
        _tenant(slug="torch-thc", name="Torch THC"),
        _tenant(slug="keee-torch-thc", request_url_name="keee-torch-thc"),
        _tenant(name="Torch THC"),
    )
    for tenant in torch_slugs:
        kept = omit_torch_recap_skips(shared, tenant)
        lowered = {email.lower() for email in kept}
        assert "junior@igniteproductions.co" not in lowered
        assert "kyle@igniteproductions.co" in lowered
        for standing in ALWAYS_INCLUDE_INTERNAL_EMAILS:
            assert standing in lowered

    famus = _tenant(slug="famus-energy", request_url_name="famus-energy", name="FAMUS Energy")
    liquid = _tenant(slug="liquid-death", name="Liquid Death")
    assert omit_torch_recap_skips(shared, famus) == shared
    assert omit_torch_recap_skips(shared, liquid) == shared
    assert omit_torch_recap_skips(shared, None) == shared

    # Non-recap Torch suppression still only drops Baesha.
    assert "junior@igniteproductions.co" not in TORCH_SUPPRESS_EMAILS
    assert "junior@igniteproductions.co" in TORCH_RECAP_SKIP_EMAILS
    assert suppress_torch_emails(shared, tenant=torch_slugs[0])[0] == shared[0]

    # Request approved still addresses him. Recap submit does not.
    to_approved, _cc = torch_request_approved_lists("Junior@igniteproductions.co")
    assert to_approved == ["Junior@igniteproductions.co"]
    to_recap, cc_recap = torch_recap_submit_lists(
        ["Junior@igniteproductions.co", "buyer@store.com"]
    )
    assert to_recap == ["buyer@store.com"]
    assert "junior@igniteproductions.co" not in {e.lower() for e in cc_recap}
    only_junior, cc_only = torch_recap_submit_lists(["Junior@igniteproductions.co"])
    assert only_junior == [TORCH_RECAP_SUBMIT_CC[0]]
    assert "junior@igniteproductions.co" not in {e.lower() for e in cc_only}

    # Field-marketing plan mail and the Famus event-submission skip stay put.
    assert "junior@igniteproductions.co" in TORCH_PLAN_SUBMIT_INTERNAL_EMAILS
    assert FAMUS_EVENT_SUBMISSION_SKIP_EMAILS == frozenset(
        {"junior@igniteproductions.co"}
    )
