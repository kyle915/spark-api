from events.outbound_policy import apply_outbound_policy
from events.torch_portal import TORCH_SUPPRESS_EMAILS


def test_apply_outbound_policy_strips_torch_suppress_for_torch():
    class T:
        slug = "torch-thc"
        request_url_name = "keee-torch-thc"
        name = "Torch THC"

    emails = [
        "kyle@igniteproductions.co",
        "baesha@igniteproductions.co",
        "liberty@torchdrinks.com",
    ]
    out = apply_outbound_policy(emails, tenant=T(), kind="generic")
    assert "baesha@igniteproductions.co" not in [e.lower() for e in out]
    assert any("kyle@" in e.lower() for e in out)
    assert "baesha@igniteproductions.co" in TORCH_SUPPRESS_EMAILS


def test_apply_outbound_policy_keeps_baesha_on_non_torch():
    class T:
        slug = "famus-energy"
        request_url_name = "famus-energy"
        name = "Famus Energy"

    emails = ["baesha@igniteproductions.co", "events@igniteproductions.co"]
    out = apply_outbound_policy(emails, tenant=T(), kind="generic")
    assert any("baesha@" in e.lower() for e in out)
