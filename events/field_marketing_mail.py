"""Emails fired when a field marketing plan is submitted.

Opt-in per brand: a brand only gets these emails when it has an entry in
``PLAN_SUBMIT_MAIL_BY_SLUG``. Torch is the only brand today.

Two emails per submitted plan:
  * a confirmation to the logged-in submitter (brand colors, client host link)
  * a notification to the brand's internal Ignite list (Spark lime, admin link)

Staffed plans also create an Event Activation request, which already alerts
the whole Ignite team. That alert skips the internal list, because those
people get the plan notification for the same submission.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings

from events import models
from events.envelopes import ClientRequestCreatedNotificationMailer, _admin_request_url
from events.mutations import _get_request_cc_emails
from utils.mailer import Envelope, Mailer

logger = logging.getLogger(__name__)

TORCH_PLAN_SUBMIT_INTERNAL_EMAILS: tuple[str, ...] = (
    "events@igniteproductions.co",
    "kyle@igniteproductions.co",
    "junior@igniteproductions.co",
    "keis@igniteproductions.co",
    "harris@igniteproductions.co",
    "nevena@igniteproductions.co",
)

CLIENT_PORTAL_URL = "https://client.igniteproductions.co"
ADMIN_PORTAL_URL = "https://admin.igniteproductions.co"
FROM_EMAIL = "Spark by Ignite <no-reply@igniteproductions.co>"
SPARK_LIME = "#c5f546"
_INK = "#0a0d09"
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RETIRED_HOSTS = frozenset(
    {"spark.igniteproductions.co", "spark-admin.web.app", "spark-new-admin.web.app"}
)


@dataclass(frozen=True)
class PlanSubmitMail:
    brand_label: str
    internal_emails: tuple[str, ...]


TORCH_PLAN_SUBMIT_MAIL = PlanSubmitMail(
    brand_label="Torch THC",
    internal_emails=TORCH_PLAN_SUBMIT_INTERNAL_EMAILS,
)

PLAN_SUBMIT_MAIL_BY_SLUG: dict[str, PlanSubmitMail] = {
    "torch": TORCH_PLAN_SUBMIT_MAIL,
    "torch-thc": TORCH_PLAN_SUBMIT_MAIL,
    "keee-torch-thc": TORCH_PLAN_SUBMIT_MAIL,
}


def plan_submit_mail_for(tenant) -> PlanSubmitMail | None:
    """Exact slug first, then exact request_url_name. None = brand not opted in."""
    slug = (getattr(tenant, "slug", None) or "").strip().lower()
    if slug in PLAN_SUBMIT_MAIL_BY_SLUG:
        return PLAN_SUBMIT_MAIL_BY_SLUG[slug]
    url_name = (getattr(tenant, "request_url_name", None) or "").strip().lower()
    return PLAN_SUBMIT_MAIL_BY_SLUG.get(url_name)


def _portal_url(setting_name: str, canonical: str) -> str:
    base = (getattr(settings, setting_name, "") or "").strip().rstrip("/")
    host = (urlparse(base).hostname or "").lower()
    if not base.startswith("https://") or host in _RETIRED_HOSTS:
        return canonical
    return base


def client_plan_url() -> str:
    return f"{_portal_url('CLIENT_FRONTEND_URL', CLIENT_PORTAL_URL)}/field-marketing"


def admin_plan_url() -> str:
    return f"{_portal_url('ADMIN_FRONTEND_URL', ADMIN_PORTAL_URL)}/field-marketing"


def brand_accent(tenant) -> str | None:
    """Tenant primary from its theme, hex only (email clients can't do oklch)."""
    try:
        theme = tenant.themes.first()
    except Exception:
        return None
    css = getattr(theme, "css_variables", None) or {}
    if not isinstance(css, dict):
        return None
    for key in ("--color-primary", "--p", "primary", "--recap-brand"):
        raw = css.get(key)
        if isinstance(raw, str) and _HEX_COLOR.match(raw.strip()):
            return raw.strip()
    return None


def _ink_on(hex_color: str) -> str:
    """Dark or white text, whichever reads on the accent."""
    raw = hex_color.lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    r, g, b = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return _INK if luminance > 0.6 else "#ffffff"


def submitter_name(user) -> str:
    full = (user.get_full_name() or "").strip() if user else ""
    return full or (getattr(user, "email", None) or "").strip() or "A brand teammate"


def _normalize(emails) -> list[str]:
    out: list[str] = []
    for raw in emails or []:
        email = (raw or "").strip().lower()
        if email and email not in out:
            out.append(email)
    return out


@dataclass(frozen=True)
class PlanMailContent:
    """What both emails show about one submitted plan."""

    brand_label: str
    plan_name: str
    rows: list[tuple[str, str]]
    request_code: str | None
    request_url: str
    submitter_name: str
    submitter_email: str
    greeting_name: str


class PlanSubmittedConfirmationMailer(Mailer):
    """To the person who submitted the plan. Brand colors, client host link."""

    def _build_logo_attachment(self):
        return None

    def __init__(self, content: PlanMailContent, to_email: str, accent: str | None):
        self.content = content
        self.to_email = to_email
        self.accent = accent or _INK

    def envelope(self) -> Envelope:
        c = self.content
        return Envelope(
            subject=f"{c.brand_label}: your field marketing plan was submitted",
            template="events.templates.emails.field_marketing_plan_submitted",
            from_email=FROM_EMAIL,
            to_emails=[self.to_email],
            headers={"Reply-To": "events@igniteproductions.co"},
            context={
                "c": c,
                "accent": self.accent,
                "accent_ink": _ink_on(self.accent),
                "plan_url": client_plan_url(),
            },
        )


class PlanSubmittedInternalMailer(Mailer):
    """To the brand's internal Ignite list. Spark lime, admin links."""

    def _build_logo_attachment(self):
        return None

    def __init__(self, content: PlanMailContent, to_emails: list[str]):
        self.content = content
        self.to_emails = to_emails

    def envelope(self) -> Envelope:
        c = self.content
        headers = {}
        if c.submitter_email:
            headers["Reply-To"] = c.submitter_email
        return Envelope(
            subject=(
                f"New {c.brand_label} field marketing plan: {c.plan_name} "
                f"(submitted by {c.submitter_name})"
            ),
            template="events.templates.emails.field_marketing_plan_submitted_internal",
            from_email=FROM_EMAIL,
            to_emails=self.to_emails,
            headers=headers,
            context={
                "c": c,
                "accent": SPARK_LIME,
                "accent_ink": _INK,
                "plan_url": admin_plan_url(),
            },
        )


def _alert_ignite_team(request: models.Request, exclude: set[str]) -> None:
    """The generic new-request alert, minus anyone who already got the plan email."""
    team = [e for e in _get_request_cc_emails() if e.strip().lower() not in exclude]
    if not team:
        return
    ClientRequestCreatedNotificationMailer(
        request=request,
        location=None,
        to_emails=team,
        auto_approved=False,
    ).send()


def notify_plan_submitted(
    event: models.FieldMarketingEvent,
    actor,
    rows: list[tuple[str, str]],
    request_code: str | None,
) -> None:
    """Send the plan-submitted emails. Never raises: the plan is already saved."""
    config = plan_submit_mail_for(event.tenant)
    internal = _normalize(config.internal_emails) if config else []
    request = event.request
    if config:
        submitter_email = (getattr(actor, "email", None) or "").strip()
        content = PlanMailContent(
            brand_label=config.brand_label,
            plan_name=event.name,
            rows=rows,
            request_code=request_code,
            request_url=_admin_request_url(request) if request else "",
            submitter_name=submitter_name(actor),
            submitter_email=submitter_email,
            greeting_name=(getattr(actor, "first_name", None) or "").strip() or "there",
        )
        if submitter_email and submitter_email.lower() not in internal:
            try:
                PlanSubmittedConfirmationMailer(
                    content, submitter_email, brand_accent(event.tenant)
                ).send()
            except Exception:
                logger.exception("Plan confirmation email failed for plan %s", event.uuid)
        if internal:
            try:
                PlanSubmittedInternalMailer(content, internal).send()
            except Exception:
                logger.exception("Plan internal email failed for plan %s", event.uuid)
    if request is not None:
        try:
            _alert_ignite_team(request, set(internal))
        except Exception:
            logger.exception("Ignite alert failed for request %s", request.id)
