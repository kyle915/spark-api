"""Client recap sign-off (Looks good / Need more photos)."""

from django.test import RequestFactory
from django.utils import timezone as dj_tz

from recaps.client_signoff import LOOKS_GOOD, NEED_MORE_PHOTOS, apply_signoff
from recaps.recap_tokens import make_recap_token
from recaps import share_views


class _Recap:
    def __init__(self):
        self.client_signoff_status = ""
        self.client_signoff_comment = ""
        self.client_signoff_at = None
        self.updated_at = None
        self.saved = False

    def save(self, update_fields=None):
        self.saved = True
        self.update_fields = update_fields


def test_apply_signoff_looks_good():
    recap = _Recap()
    apply_signoff(recap, status=LOOKS_GOOD, comment="  ship it  ")
    assert recap.client_signoff_status == LOOKS_GOOD
    assert recap.client_signoff_comment == "ship it"
    assert recap.client_signoff_at is not None
    assert recap.saved


def test_apply_signoff_rejects_unknown_status():
    recap = _Recap()
    try:
        apply_signoff(recap, status="maybe", comment="")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert recap.saved is False


def test_public_signoff_rejects_bad_token():
    req = RequestFactory().post(
        "/api/public/recap/not-a-token/signoff",
        data=b'{"status":"looks_good"}',
        content_type="application/json",
    )
    resp = share_views.public_recap_signoff_view(req, "not-a-token")
    assert resp.status_code == 400


def test_public_signoff_url_is_mounted():
    from django.urls import reverse

    token = make_recap_token("legacy", 1)
    assert reverse("recaps.public_recap_signoff", args=[token]).endswith(
        f"/recap/{token}/signoff"
    )


def test_need_more_photos_emails_and_pushes_ambassador(monkeypatch):
    from recaps.client_signoff import NEED_MORE_PHOTOS, notify_ambassador_need_photos

    envelopes = []
    pushes = []

    monkeypatch.setattr(
        "utils.mailer.Mailer.send_now",
        lambda self: envelopes.append(self.envelope()),
    )
    monkeypatch.setattr(
        "ambassadors.push._send_push_to_user_sync",
        lambda user_id, **kw: pushes.append((user_id, kw)) or 0,
    )

    class _User:
        id = 9
        email = "ba@example.com"

    class _Amb:
        user = _User()
        email = None
        user_id = 9

    class _Event:
        id = 1
        name = "Costco Austin"
        uuid = "evt-1"

    class _Recap:
        name = "LD recap"
        ambassador = _Amb()
        event = _Event()
        client_signoff_status = NEED_MORE_PHOTOS
        client_signoff_comment = "need store shots"

    notify_ambassador_need_photos(_Recap())
    assert len(envelopes) == 1
    assert envelopes[0].to_emails == ["ba@example.com"]
    assert "More photos" in envelopes[0].subject
    assert pushes == [
        (
            9,
            {
                "title": "More photos needed",
                "body": "The client asked for more photos on Costco Austin. Note: need store shots",
                "data": {"screen": "recap", "eventUuid": "evt-1"},
            },
        )
    ]


def test_ops_need_photos_notifies_ambassador_even_without_admins(monkeypatch):
    from recaps.client_signoff import NEED_MORE_PHOTOS, notify_ops_signoff

    called = []
    monkeypatch.setattr(
        "recaps.client_signoff.notify_ambassador_need_photos",
        lambda recap: called.append(recap),
    )
    monkeypatch.setattr(
        "events.mutations._get_spark_admin_emails",
        lambda: [],
    )

    class _Recap:
        name = "LD recap"
        client_signoff_status = NEED_MORE_PHOTOS
        client_signoff_comment = ""
        event = None

    recap = _Recap()
    notify_ops_signoff(recap, kind="legacy")
    assert called == [recap]


def test_ops_looks_good_does_not_notify_ambassador(monkeypatch):
    from recaps.client_signoff import LOOKS_GOOD, notify_ops_signoff

    called = []
    monkeypatch.setattr(
        "recaps.client_signoff.notify_ambassador_need_photos",
        lambda recap: called.append(recap),
    )
    monkeypatch.setattr(
        "events.mutations._get_spark_admin_emails",
        lambda: [],
    )

    class _Recap:
        name = "LD recap"
        client_signoff_status = LOOKS_GOOD
        client_signoff_comment = ""
        event = None

    notify_ops_signoff(_Recap(), kind="legacy")
    assert called == []
