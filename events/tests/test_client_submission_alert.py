"""Unit tests for ClientRequestCreatedNotificationMailer.envelope() — the
Ignite-team heads-up fired the moment a client files a request from their own
portal login (see events.mutations._notify_spark_admins_for_client_request).

Guards two things that are easy to regress:
  * the subject carries the tenant name + REQ id so it's scannable in an inbox
  * the `auto_approved` flag threads into the template context, which toggles
    the banner between amber "awaiting your approval" and green "auto-approved"
    — the client self-serve path auto-approves, so the banner MUST tell the
    truth there.
No DB — fake request objects, matching test_request_email_products.py.
"""

import datetime

from events.envelopes import ClientRequestCreatedNotificationMailer


class _Tenant:
    def __init__(self, name):
        self.name = name


class _Manager:
    def select_related(self, *args, **kwargs):
        return self

    def all(self):
        return []


class _Request:
    def __init__(self, id=42, tenant_name="Girl Beer"):
        self.id = id
        self.tenant = _Tenant(tenant_name)
        self.request_product = _Manager()
        self.date = datetime.datetime(2026, 7, 4, 9, 0)
        self.start_time = datetime.datetime(2026, 7, 4, 9, 0)
        self.end_time = datetime.datetime(2026, 7, 4, 13, 0)
        self.timezone = None


def _envelope(*, id=42, tenant_name="Girl Beer", auto_approved=False):
    mailer = ClientRequestCreatedNotificationMailer(
        request=_Request(id=id, tenant_name=tenant_name),
        location=None,
        to_emails=["team@igniteproductions.co"],
        auto_approved=auto_approved,
    )
    return mailer.envelope()


def test_subject_carries_tenant_and_req_id():
    assert _envelope(id=42, tenant_name="Girl Beer").subject == (
        "New client submission · Girl Beer · REQ-42"
    )


def test_subject_without_req_id():
    assert _envelope(id=None, tenant_name="Girl Beer").subject == (
        "New client submission · Girl Beer"
    )


def test_subject_defaults_tenant_when_missing():
    mailer = ClientRequestCreatedNotificationMailer(
        request=_Request(id=None, tenant_name=None),
        location=None,
        to_emails=["team@igniteproductions.co"],
    )
    assert mailer.envelope().subject == "New client submission · Client"


def test_auto_approved_flag_threads_into_context():
    assert _envelope(auto_approved=True).context["auto_approved"] is True
    assert _envelope(auto_approved=False).context["auto_approved"] is False


def test_recipients_preserved():
    assert _envelope().to_emails == ["team@igniteproductions.co"]
