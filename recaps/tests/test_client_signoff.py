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
