"""Public recap comment thread: approved-only, token-gated, notifies ops."""

import json

import pytest
from django.test import RequestFactory

from recaps import share_views
from recaps.models import RecapComment
from recaps.recap_comments import create_comment
from recaps.recap_tokens import make_recap_token


class _Recap:
    pk = 7
    id = 7


def test_create_comment_trims_and_validates():
    # Unit-level validation without DB create: empty body raises first.
    with pytest.raises(ValueError):
        create_comment(_Recap(), kind="legacy", body="   ")
    with pytest.raises(ValueError):
        create_comment(_Recap(), kind="legacy", body="x" * 2001)
    with pytest.raises(ValueError):
        create_comment(_Recap(), kind="legacy", body="hi", author_email="nope")


def test_public_comment_rejects_bad_token():
    req = RequestFactory().post(
        "/api/public/recap/not-a-token/comments",
        data=b'{"body":"hi"}',
        content_type="application/json",
    )
    resp = share_views.public_recap_comment_view(req, "not-a-token")
    assert resp.status_code == 400


def test_public_comment_url_is_mounted():
    from django.urls import resolve, reverse

    token = make_recap_token("legacy", 1)
    assert reverse("recaps.public_recap_comment", args=[token]).endswith(
        f"/recap/{token}/comments"
    )
    match = resolve(f"/api/public/recap/{token}/comments")
    assert match.func is share_views.public_recap_comment_view


from ambassadors.tests.base import AmbassadorsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestPublicCommentGate(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from events import models as em
        from recaps import models as rm

        self.setup_default_roles()
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Comment Tenant")
        self.event = em.Event.objects.create(
            name="Shift", tenant=self.tenant, address="1 Main", created_by=self.sys
        )
        self.recap = rm.Recap.objects.create(
            name="Approved recap", event=self.event, created_by=self.sys,
            updated_by=self.sys, approved=True, total_engagements=5,
        )
        self.draft = rm.Recap.objects.create(
            name="Draft recap", event=self.event, created_by=self.sys,
            updated_by=self.sys, approved=False, total_engagements=5,
        )

    def _post(self, recap_id, body_dict, monkeypatch):
        monkeypatch.setattr(
            "recaps.share_views.load_shared_recap",
            lambda kind, rid: self.recap if rid == self.recap.pk else self.draft,
        )
        # notify is best-effort mail — stub it out of the DB test.
        import recaps.recap_comments as rc

        monkeypatch.setattr(rc, "notify_ops_comment", lambda *a, **k: None)
        token = make_recap_token("legacy", recap_id)
        req = RequestFactory().post(
            f"/api/public/recap/{token}/comments",
            data=json.dumps(body_dict).encode(),
            content_type="application/json",
        )
        return share_views.public_recap_comment_view(req, token)

    def test_comment_on_approved_recap_is_stored_and_returned(self, monkeypatch):
        resp = self._post(self.recap.pk, {"body": "  Can we get the shelf photo? ", "authorName": "Sam"}, monkeypatch)
        assert resp.status_code == 201
        saved = RecapComment.objects.get(recap=self.recap)
        assert saved.body == "Can we get the shelf photo?"
        assert saved.author_name == "Sam"
        payload = json.loads(resp.content)
        assert payload["recap"]["comments"][0]["body"] == "Can we get the shelf photo?"

    def test_comment_on_unapproved_recap_is_404_and_stores_nothing(self, monkeypatch):
        resp = self._post(self.draft.pk, {"body": "hello?"}, monkeypatch)
        assert resp.status_code == 404
        assert RecapComment.objects.count() == 0
