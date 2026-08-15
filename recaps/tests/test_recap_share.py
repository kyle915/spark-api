"""Share-token round-trip + public recap URL / view contract."""

from django.test import RequestFactory
from django.urls import resolve, reverse

from recaps import share_views
from recaps.recap_tokens import (
    BadSignature,
    make_recap_token,
    verify_recap_token,
)
from recaps.share_render import recap_to_public_dict


def test_recap_token_round_trip_legacy():
    token = make_recap_token("legacy", 4242)
    assert verify_recap_token(token) == ("legacy", 4242)


def test_recap_token_round_trip_custom():
    token = make_recap_token("custom", 7)
    assert verify_recap_token(token) == ("custom", 7)


def test_recap_token_accepts_max_age_none():
    token = make_recap_token("legacy", 9)
    assert verify_recap_token(token, max_age=None) == ("legacy", 9)


def test_recap_token_rejects_tampered_token():
    token = make_recap_token("custom", 1)
    try:
        verify_recap_token(token + "tamper")
        raise AssertionError("expected BadSignature")
    except BadSignature:
        pass


def test_recap_token_rejects_report_token_replay():
    from recaps.report_tokens import make_report_token

    report_token = make_report_token(11)
    try:
        verify_recap_token(report_token)
        raise AssertionError("expected BadSignature")
    except BadSignature:
        pass


def test_public_recap_urls_mount_share_views():
    """GET recap/<token> and recap/<token>/pdf must hit share_views.

    A prior placeholder wired the PDF path to the campaign-report view
    and omitted the JSON path. Both must resolve to share_views.
    """
    json_match = resolve("/api/public/recap/dummy-token")
    pdf_match = resolve("/api/public/recap/dummy-token/pdf")
    assert json_match.func is share_views.public_recap_view
    assert pdf_match.func is share_views.public_recap_pdf_view
    assert reverse("recaps.public_recap", args=["tok"]) == "/api/public/recap/tok"
    assert (
        reverse("recaps.public_recap_pdf", args=["tok"])
        == "/api/public/recap/tok/pdf"
    )


def test_public_recap_view_rejects_bad_token():
    req = RequestFactory().get("/api/public/recap/not-a-token")
    resp = share_views.public_recap_view(req, "not-a-token")
    assert resp.status_code == 400


def test_public_recap_pdf_view_rejects_bad_token():
    req = RequestFactory().get("/api/public/recap/not-a-token/pdf")
    resp = share_views.public_recap_pdf_view(req, "not-a-token")
    assert resp.status_code == 400


def test_public_recap_view_rejects_report_token_replay():
    from recaps.report_tokens import make_report_token

    token = make_report_token(11)
    req = RequestFactory().get(f"/api/public/recap/{token}")
    resp = share_views.public_recap_view(req, token)
    assert resp.status_code == 400


class _Mgr:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_recap_to_public_dict_legacy_shape_omits_share_token():
    recap = _Obj(
        name="Store recap",
        approved=True,
        products_sold=4,
        external_ba_name="Pat",
        ambassador=None,
        retailer=_Obj(name="Whole Foods"),
        event=_Obj(
            name="Saturday sampling",
            date=None,
            address="1 Main St",
            retailer=None,
            location=None,
            state=_Obj(name="TX", code="TX"),
            request=None,
        ),
        location=None,
        state=None,
        recap_files=_Mgr([]),
        product_samples=_Mgr([]),
        consumer_engagements=_Mgr(
            [_Obj(total_consumer=12, first_time_consumers=3,
                  brand_aware_consumers=8, willing_to_purchase_consumers=5)]
        ),
        custom_recap_template=None,
        event_date=None,
        created_at=None,
    )
    out = recap_to_public_dict("legacy", recap)
    assert out["kind"] == "legacy"
    assert out["name"] == "Store recap"
    assert out["approved"] is True
    assert out["retailer"] == "Whole Foods"
    assert "shareToken" not in out
    labels = {k["label"] for k in out["kpis"]}
    assert "Samples" in labels
    assert "Products sold" in labels
