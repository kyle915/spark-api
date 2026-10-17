"""The auto-approved requestor email sub copy is one sentence.

The eyebrow and "You're on, {name}." headline stay. The follow-on sentence
about staffing a BA and a calendar invite is not part of that sub copy.
The named-approver and brand-POC variants keep their own wording, and the
"What happens next" list below the sub copy stays.
"""

from django.template.loader import render_to_string

from utils.mailer import html_to_text

TEMPLATE = "emails/request_approved_requestor_v2.html"

STAFFING_SENTENCE = (
    "Ignite will staff a BA and you'll get a calendar invite shortly."
)


def _render(**extra):
    context = {
        "requestor_name": "LeslyAnn Altet",
        "first_name": "LeslyAnn",
        "auto_approved": False,
        "reviewed_by_name": "",
    }
    context.update(extra)
    return render_to_string(TEMPLATE, context)


def _flat(html: str) -> str:
    return " ".join(html.split())


def test_auto_approved_subcopy_is_one_sentence():
    html = _render(auto_approved=True)
    text = html_to_text(html)

    assert "REQUEST APPROVED" in html
    assert "You're on, LeslyAnn Altet." in html
    assert "This sampling request is approved." in html
    assert STAFFING_SENTENCE not in _flat(html)
    assert STAFFING_SENTENCE not in " ".join(text.split())
    assert "This sampling request is approved." in text
    # Body sections under the sub copy stay.
    assert "Ignite staffs a brand ambassador for the activation." in html
    assert "A calendar invite goes to you and the brand POC." in html


def test_named_approver_subcopy_is_unchanged():
    html = _render(reviewed_by_name="Kyle Christiansen")
    assert "Kyle Christiansen" in html
    assert "approved your activation request." in html
    assert STAFFING_SENTENCE in _flat(html)
    assert "This sampling request is approved." not in html


def test_brand_poc_subcopy_is_unchanged():
    html = _render()
    assert "The brand POC" in html
    assert "approved your activation request." in html
    assert STAFFING_SENTENCE in _flat(html)
    assert "This sampling request is approved." not in html
