"""Junior stays on shared lists. Torch recap notifications drop him."""

from types import SimpleNamespace

from recaps.client_signoff import LOOKS_GOOD, notify_ops_signoff
from recaps.mutation_parts.notify import _send_recap_data_quality_alert
from recaps.recap_comments import notify_ops_comment

JUNIOR = "Junior@igniteproductions.co"
SHARED = [
    JUNIOR,
    "kyle@igniteproductions.co",
    "amiel@igniteproductions.co",
]


def _tenant(slug: str, name: str):
    return SimpleNamespace(slug=slug, request_url_name=slug, name=name)


def _recap(tenant, *, name="Recap"):
    return SimpleNamespace(
        id=9,
        name=name,
        uuid="01900000-0000-7000-8000-00000000c0ff",
        client_signoff_status=LOOKS_GOOD,
        client_signoff_comment="",
        external_ba_name="Ada",
        ambassador=None,
        tenant=tenant,
        event=SimpleNamespace(tenant=tenant, name="Store", rmm_asigned=None),
    )


def test_signoff_comment_and_suspect_numbers_drop_junior_for_torch_only(monkeypatch):
    envelopes: list = []

    def capture(self):
        envelopes.append(self.envelope())

    monkeypatch.setattr("utils.mailer.Mailer.send_now", capture)
    monkeypatch.setattr(
        "events.mutations._get_spark_admin_emails",
        lambda: list(SHARED),
    )
    monkeypatch.setattr(
        "tenants.support._resolve_ignite_recipients",
        lambda: list(SHARED),
    )

    torch = _tenant("torch", "Torch")
    famus = _tenant("famus-energy", "FAMUS Energy")
    comment = SimpleNamespace(author_name="Client", author_email="", body="Question")

    notify_ops_signoff(_recap(torch), kind="legacy")
    notify_ops_comment(_recap(torch), kind="legacy", comment=comment)
    _send_recap_data_quality_alert(_recap(torch), ["conversion over 100%"])
    notify_ops_signoff(_recap(famus, name="Famus recap"), kind="legacy")
    notify_ops_comment(_recap(famus), kind="legacy", comment=comment)
    _send_recap_data_quality_alert(_recap(famus), ["conversion over 100%"])

    assert len(envelopes) == 6
    for envelope in envelopes[:3]:
        assert "junior@igniteproductions.co" not in {
            email.lower() for email in envelope.to_emails
        }
        assert "amiel@igniteproductions.co" in envelope.to_emails
        assert "kyle@igniteproductions.co" in envelope.to_emails
    for envelope in envelopes[3:]:
        assert "junior@igniteproductions.co" in {
            email.lower() for email in envelope.to_emails
        }

    assert envelopes[0].subject.startswith("Recap sign-off —")
    assert envelopes[1].subject.startswith("Recap question —")
    assert "suspect numbers" in envelopes[2].subject
